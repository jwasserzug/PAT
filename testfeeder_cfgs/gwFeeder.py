#!/usr/bin/python

# pylint: disable=line-too-long, invalid-name, missing-docstring, too-few-public-methods


import collections
import itertools
import os, sys
os.environ["OSCAR_ENABLE_CISEFIX"] = "1"
import random
from optparse import OptionParser
from decimal import Decimal
import random
from OSCAR import common
import time
from threading import Lock
import copy

try:
    import numpy as Numeric
except:
    import Numeric

from testfeeders.histogram import Histogram
from testfeeders.perftest import IsefixPerformanceTest, FeedStep, MDDMessageTracker
from testfeeders.perftest import Product, GatewaySession, GatewaySessionConfig
from testfeeders.refdata import ReferenceData
from OSCAR import isefix
from OSCAR import MsgIO

LAZY_CFG_DIR = "/opt/gts/usr/share/doc/testfeeders/gwFeeder/" # location of default "lazy mode" configuration files

SUCESS_MSG_TYPES = (isefix.OrderAcknowledge, isefix.MassQuoteAcknowledgement,
        isefix.AddComplexInstrumentResponse, isefix.DeleteAllQuotesResponse)

# response message types that are produced first without a specific product ID as
# confirmation, and then one per affected product
SUCESS_MSG_TYPES_PRODUCT = (isefix.QuoteActionReport, isefix.DeleteAllOrdersResponse)

def floatToInteger1E8(val):
    # setting prices can't be directly made by using Decimal class, as it's too slow and
    # would limit feeding throughput. Instead, they're set by converting the float to an int * 1E8, 
    # and setting them in _Price shortcuts in the messages. However float can't be used
    # directly, as the operation can produce a different integer than expected due to the
    # fact that float will keep the closest representable value. As a workaround, the resulting
    # float will be rounded at the less significant digit of the integer part.
    return int(round(val * 1E8, -1))

class BusinessUnit:

    def __init__(self, buId):
        self.id = buId
        self.users = []

    def getRandomUser(self):
        return random.choice(self.users)


class MassQuoteFactory(FeedStep):

    def __init__(self, products, clOrdIdGen, minQuoteSize, maxQuoteSize, quantityMin, quantityMax, quantityStep, massQuoteSpread=None, productComplex=None, singleSidedPercent=0):
        self.products = products
        self.products = [product for product in products if len(product.instruments) >= minQuoteSize]
        self.clOrdIdGen = clOrdIdGen
        self.minQuoteSize = minQuoteSize
        self.maxQuoteSize = maxQuoteSize
        self.quantityMin = quantityMin
        self.quantityMax = quantityMax
        self.quantityStep = quantityStep
        self.massQuoteSpread = massQuoteSpread
        self.productComplex = productComplex
        self.singleSidedPercent = singleSidedPercent
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        quantityGen = self.randomIntegerGenerator(self.quantityMin, self.quantityMax + self.quantityStep, self.quantityStep)
        numQuotesGen = self.randomIntegerGenerator(self.minQuoteSize, self.maxQuoteSize + 1)
        massQuoteMsg = isefix.MassQuote()
        massQuoteMsg.RequestHeader.MsgType = isefix.MsgType.MassQuote
        while True:
            numQuoteEntries = numQuotesGen.next()
            product = random.choice(self.products)
            massQuoteMsg.QuoteID = self.clOrdIdGen.next()
            massQuoteMsg.QuotSetGrp = [isefix.QuotSetGrpComp()]
            massQuoteMsg.QuotSetGrp[0].MarketSegmentID = str(product.id)
            singleSidedCnt = (numQuoteEntries * self.singleSidedPercent) / 100
            for instrument in product.getRandomInstruments(numQuoteEntries):
                quotEntryGrp = isefix.QuotEntryGrpComp()
                quotEntryGrp.SecurityID = str(instrument)
                price = product.nextPrice(instrument)
                quotEntryGrp._BidPx = floatToInteger1E8(price[0])
                quotEntryGrp.BidSize = quantityGen.next()
                if self.massQuoteSpread:
                    quotEntryGrp._OfferPx = floatToInteger1E8(price[0] + self.massQuoteSpread)
                else:
                    quotEntryGrp._OfferPx = floatToInteger1E8(price[1])
                quotEntryGrp.OfferSize = quantityGen.next()
                quotEntryGrp.ProductComplex = self.productComplex
                if singleSidedCnt > 0:
                    if singleSidedCnt % 2 == 0:
                        quotEntryGrp.BidPx = None
                        quotEntryGrp.BidSize = None
                    else:
                        quotEntryGrp.OfferPx = None
                        quotEntryGrp.OfferSize = None
                    singleSidedCnt -= 1
                massQuoteMsg.QuotSetGrp[0].QuotEntryGrp.append(quotEntryGrp)
            yield massQuoteMsg


class TradesFactory(FeedStep):

    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, price=60, dayOrders=False):
        self.products = products
        self.sessionConfig = sessionConfig
        self.clOrdIdGen = clOrdIdGen
        self.mmUnits = mmUnits
        self.rawPrice = floatToInteger1E8(price)
        self.dayOrders = dayOrders
        FeedStep.__init__(self, None)

    def setCurrentSessionConfig(self, sessionConfig):
        self.sessionConfig = sessionConfig

    @property
    def currentBUID(self):
        return self.sessionConfig.memberID

    @property
    def currentUserID(self):
        return self.sessionConfig.userID

    def setClearingOrderCapacity(self, msg):
        if self.currentBUID in self.mmUnits:
            msg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
            msg.OrderCapacity = isefix.OrderCapacity.Market_Maker
        else:
            msg.ClearingCapacity = isefix.ClearingCapacity.Customer
            msg.OrderCapacity = isefix.OrderCapacity.Customer

    def messageGenerator(self):
        orderAddMsg = isefix.NewOrderSingle()
        orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderSingle
        orderAddMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderAddMsg.OrdType = isefix.OrdType.Limit
        orderAddMsg.PositionEffect = isefix.PositionEffect.Open
        orderAddMsg.TransactTime = common.datetime.now()
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderAddMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader
        while True:
            product = random.choice(self.products)
            instrument = product.getRandomInstrument()
            self.setClearingOrderCapacity(orderAddMsg)
            orderAddMsg.ClOrdID = self.clOrdIdGen.next()
            orderAddMsg.Parties[0].PartyID = str(self.currentBUID)
            orderAddMsg.Parties[1].PartyID = str(self.currentUserID)
            orderAddMsg.Side = isefix.Side.Sell
            if self.dayOrders:
                orderAddMsg.TimeInForce = isefix.TimeInForce.Day
            else:
                orderAddMsg.TimeInForce = isefix.TimeInForce.GTC
            orderAddMsg.MarketSegmentID = str(product.id)
            orderAddMsg.SecurityID = str(instrument)
            orderAddMsg._Price = self.rawPrice
            orderAddMsg.OrderQty = 1000
            yield orderAddMsg
            for i in xrange(100):
                self.setClearingOrderCapacity(orderAddMsg)
                orderAddMsg.Parties[0].PartyID = str(self.currentBUID)
                orderAddMsg.Parties[1].PartyID = str(self.currentUserID)
                orderAddMsg.ClOrdID = self.clOrdIdGen.next()
                orderAddMsg.Side = isefix.Side.Buy
                orderAddMsg.OrderQty = 10
                yield orderAddMsg

class OrderAddFactory(FeedStep):

    PRICE_PROTECTION = {'LOCAL': '1', 'NATIONAL': '2'}

    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, IOCPercent, quantityMin, quantityMax, quantityStep, sendOpposite=False, buyRatio=1, sellRatio=1, instrumentLegs=None, persist=True, allOrNone=False, marketPrice=False, dayOrders=False, priceProtectionScope=None):
        self.IOCPercent = IOCPercent
        self.products = products
        self.clOrdIdGen = clOrdIdGen
        self.sessionConfig = sessionConfig
        self.mmUnits = mmUnits
        self.quantityMin = quantityMin
        self.quantityMax = quantityMax
        self.quantityStep = quantityStep
        self.sendOpposite = sendOpposite
        self.buyRatio = buyRatio
        self.sellRatio = sellRatio 
        self.instrumentLegs = instrumentLegs
        self.persist = persist
        self.allOrNone = allOrNone
        self.marketPrice = marketPrice
        self.dayOrders = dayOrders
        self.priceProtectionScope = self.priceProtectionValue(priceProtectionScope)
        FeedStep.__init__(self, None)

    def setCurrentSessionConfig(self, sessionConfig):
        self.sessionConfig = sessionConfig

    @property
    def currentBUID(self):
        return self.sessionConfig.memberID

    @property
    def currentUserID(self):
        return self.sessionConfig.userID

    def setClearingOrderCapacity(self, msg):
        if self.currentBUID in self.mmUnits:
            if not self.instrumentLegs:
                msg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
            msg.OrderCapacity = isefix.OrderCapacity.Market_Maker
            legClearingCapacity = isefix.LegClearingCapacity.Market_Maker
        else:
            if not self.instrumentLegs:
                msg.ClearingCapacity = isefix.ClearingCapacity.Customer
            msg.OrderCapacity = isefix.OrderCapacity.Customer
            legClearingCapacity = isefix.LegClearingCapacity.Customer

        if self.instrumentLegs:
            for legOrdGrp in msg.LegOrdGrp:
                legOrdGrp.LegClearingCapacity = legClearingCapacity

    def priceProtectionValue(self, value):
        value = str(value)
        return value if value in self.PRICE_PROTECTION.values() else self.PRICE_PROTECTION.get(value.upper())

    def messageGenerator(self):
        if self.dayOrders:
            timeInForce = isefix.TimeInForce.Day
        else:
            timeInForce = isefix.TimeInForce.GTC
        timeInForceGen = self.randomSequenceGenerator([(isefix.TimeInForce.IOC, self.IOCPercent),
                                                        (timeInForce, 100 - self.IOCPercent)])
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, self.buyRatio), (isefix.Side.Sell, self.sellRatio)])
        quantityGen = self.randomIntegerGenerator(self.quantityMin, self.quantityMax + self.quantityStep, self.quantityStep)
        execInst = []
        if self.instrumentLegs:
            orderAddMsg = isefix.NewOrderMultileg()
            orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderMultileg
        else:
            orderAddMsg = isefix.NewOrderSingle()
            orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderSingle
        orderAddMsg.PriceProtectionScope = self.priceProtectionScope
        if self.persist:
            execInst.append(isefix.ExecInst.Reinstate_on_failure)
        if self.allOrNone:
            execInst.append(isefix.ExecInst.AON)
        # If there's some values for ExecInst, then concat them with spaces between them
        if len(execInst) > 0:
            orderAddMsg.ExecInst = " ".join(execInst) 
        if self.marketPrice:
            orderAddMsg.OrdType = isefix.OrdType.Market
        else:
            orderAddMsg.OrdType = isefix.OrdType.Limit
        if not self.instrumentLegs:
            orderAddMsg.PositionEffect = isefix.PositionEffect.Open
        orderAddMsg.TransactTime = common.datetime.now()
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderAddMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader

        while True:
            product = random.choice(self.products)
            instrument = product.getRandomInstrument()

            if self.instrumentLegs:
                orderAddMsg.LegOrdGrp = []
                for i in xrange(self.instrumentLegs[instrument]["numLegs"]):
                    orderAddMsg.LegOrdGrp.append(isefix.LegOrdGrpComp())
                    orderAddMsg.LegOrdGrp[i].LegPositionEffect = isefix.LegPositionEffect.Open
                    orderAddMsg.LegOrdGrp[i].LegSecurityType = isefix.LegSecurityType.Option
                if self.instrumentLegs[instrument]["hasStockLeg"]:
                    orderAddMsg.ProductComplex = isefix.ProductComplex.Stock_combination
                    orderAddMsg.LegOrdGrp[0].LegSecurityType = isefix.LegSecurityType.Common_Stock
                else:
                    orderAddMsg.ProductComplex = isefix.ProductComplex.Standard_combination

            self.setClearingOrderCapacity(orderAddMsg)

            orderAddMsg.ClOrdID = self.clOrdIdGen.next()
            orderAddMsg.Parties[0].PartyID = str(self.currentBUID)
            orderAddMsg.Parties[1].PartyID = str(self.currentUserID)
            orderAddMsg.Side = sideGen.next()
            orderAddMsg.TimeInForce = timeInForceGen.next()
            orderAddMsg.MarketSegmentID = str(product.id)
            orderAddMsg.SecurityID = str(instrument)
            if not self.marketPrice:
                orderAddMsg._Price = floatToInteger1E8(product.nextPrice(instrument)[0])
            orderAddMsg.OrderQty = quantityGen.next()
            yield orderAddMsg
            if self.sendOpposite:
                orderAddMsg.ClOrdID = self.clOrdIdGen.next()
                self.setClearingOrderCapacity(orderAddMsg)
                if orderAddMsg.Side == isefix.Side.Buy:
                    orderAddMsg.Side = isefix.Side.Sell
                else:
                    orderAddMsg.Side = isefix.Side.Buy
                yield orderAddMsg


class OrderAddDelFactory(FeedStep):

    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, burstLen, quantityMin, quantityMax, quantityStep, dayOrders=False):
        self.burstLen = burstLen
        self.products = products
        self.sessionConfig = sessionConfig
        self.clOrdIdGen = clOrdIdGen
        self.mmUnits = mmUnits
        self.quantityMin = quantityMin
        self.quantityMax = quantityMax
        self.quantityStep = quantityStep
        self.dayOrders = dayOrders
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, 1), (isefix.Side.Sell, 1)])
        quantityGen = self.randomIntegerGenerator(self.quantityMin, self.quantityMax + self.quantityStep, self.quantityStep)

        orderAddMsg = isefix.NewOrderSingle()
        orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderSingle
        orderAddMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderAddMsg.OrdType = isefix.OrdType.Limit
        if self.dayOrders:
            orderAddMsg.TimeInForce = isefix.TimeInForce.Day
        else:
            orderAddMsg.TimeInForce = isefix.TimeInForce.GTC
        orderAddMsg.PositionEffect = isefix.PositionEffect.Open
        orderAddMsg.TransactTime = common.datetime.now()
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderAddMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader

        orderDelMsg = isefix.OrderCancelRequest()
        orderDelMsg.RequestHeader.MsgType = isefix.MsgType.OrderCancelRequest

        buID = self.sessionConfig.memberID
        user = self.sessionConfig.userID

        while True:
            product = random.choice(self.products)
            instrument = product.getRandomInstrument()
            side = sideGen.next()
            orderQty = quantityGen.next()
            if buID in self.mmUnits:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Market_Maker
            else:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Customer
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Customer
            for i in xrange(self.burstLen):
                limitPrice = product.nextPrice(instrument)
                orderAddMsg.ClOrdID = self.clOrdIdGen.next()
                orderAddMsg.Parties[0].PartyID = str(buID)
                orderAddMsg.Parties[1].PartyID = str(user)
                orderAddMsg.Side = side
                orderAddMsg.MarketSegmentID = str(product.id)
                orderAddMsg.SecurityID = str(instrument)
                orderAddMsg._Price = floatToInteger1E8(limitPrice[0])
                orderAddMsg.OrderQty = orderQty
                yield orderAddMsg
                orderDelMsg.OrigClOrdID = orderAddMsg.ClOrdID
                orderDelMsg.ClOrdID = self.clOrdIdGen.next()
                orderDelMsg.MarketSegmentID = str(product.id)
                orderDelMsg.SecurityID = str(instrument)
                yield orderDelMsg


class OrderAddModFactory(FeedStep):

    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, burstLen, quantityMin, quantityMax, quantityStep, dayOrders=False):
        self.burstLen = burstLen
        self.products = products
        self.sessionConfig = sessionConfig
        self.clOrdIdGen = clOrdIdGen
        self.mmUnits = mmUnits
        self.quantityMin = quantityMin
        self.quantityMax = quantityMax
        self.quantityStep = quantityStep
        self.dayOrders = dayOrders
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, 1), (isefix.Side.Sell, 1)])
        quantityGen = self.randomIntegerGenerator(self.quantityMin, self.quantityMax + self.quantityStep, self.quantityStep)

        orderAddMsg = isefix.NewOrderSingle()
        orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderSingle
        orderAddMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderAddMsg.OrdType = isefix.OrdType.Limit
        if self.dayOrders:
            orderAddMsg.TimeInForce = isefix.TimeInForce.Day
        else:
            orderAddMsg.TimeInForce = isefix.TimeInForce.GTC
        orderAddMsg.PositionEffect = isefix.PositionEffect.Open
        orderAddMsg.TransactTime = common.datetime.now()
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderAddMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader

        orderModMsg = isefix.OrderCancelReplaceRequest()
        orderModMsg.RequestHeader.MsgType = isefix.MsgType.OrderCancelReplaceRequest
        orderModMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderModMsg.OrdType = isefix.OrdType.Limit
        if self.dayOrders:
            orderAddMsg.TimeInForce = isefix.TimeInForce.Day
        else:
            orderModMsg.TimeInForce = isefix.TimeInForce.GTC
        orderModMsg.PositionEffect = isefix.PositionEffect.Open
        orderModMsg.TransactTime = common.datetime.now()
        orderModMsg.Parties.append(isefix.PartiesComp())
        orderModMsg.Parties.append(isefix.PartiesComp())
        orderModMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderModMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader

        buID = self.sessionConfig.memberID
        user = self.sessionConfig.userID

        while True:
            product = random.choice(self.products)
            instrument = product.getRandomInstrument()
            side = sideGen.next()
            orderQty = quantityGen.next()
            limitPrice = product.nextPrice(instrument)
            if buID in self.mmUnits:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Market_Maker
                orderModMsg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
                orderModMsg.OrderCapacity = isefix.OrderCapacity.Market_Maker
            else:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Customer
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Customer
                orderModMsg.ClearingCapacity = isefix.ClearingCapacity.Customer
                orderModMsg.OrderCapacity = isefix.OrderCapacity.Customer
            orderAddMsg.ClOrdID = self.clOrdIdGen.next()
            orderAddMsg.Parties[0].PartyID = str(buID)
            orderAddMsg.Parties[1].PartyID = str(user)
            orderAddMsg.Side = side
            orderAddMsg.MarketSegmentID = str(product.id)
            orderAddMsg.SecurityID = str(instrument)
            orderAddMsg._Price = floatToInteger1E8(limitPrice[0])
            orderAddMsg.OrderQty = orderQty
            yield orderAddMsg
            orderModMsg.OrigClOrdID = orderAddMsg.ClOrdID
            for i in xrange(self.burstLen):
                limitPrice = product.nextPrice(instrument)
                orderModMsg.ClOrdID = self.clOrdIdGen.next()
                orderModMsg.Parties[0].PartyID = str(buID)
                orderModMsg.Parties[1].PartyID = str(user)
                orderModMsg.Side = side
                orderModMsg.MarketSegmentID = str(product.id)
                orderModMsg.SecurityID = str(instrument)
                orderModMsg._Price = floatToInteger1E8(limitPrice[0])
                orderModMsg.OrderQty = orderQty
                yield orderModMsg
                orderModMsg.OrigClOrdID = orderModMsg.ClOrdID


class OrderAddModRestingFactory(FeedStep):

    def __init__(self, orderAddGen, numOrders, dayOrders=False):
        self.orderAddGen = orderAddGen
        self.numOrders = numOrders
        self.dayOrders = dayOrders
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        restingOrders = []
        orderModSingleMsg = isefix.OrderCancelReplaceRequest()
        orderModSingleMsg.RequestHeader.MsgType = isefix.MsgType.OrderCancelReplaceRequest
        orderModMultilegMsg = isefix.MultilegOrderCancelReplace()
        orderModMultilegMsg.RequestHeader.MsgType = isefix.MsgType.MultilegOrderCancelReplace
        for i in xrange(self.numOrders):
            orderAddMsg = self.orderAddGen.nextMessage()
            if self.dayOrders:
                orderAddMsg.TimeInForce = isefix.TimeInForce.Day
            else:
                orderAddMsg.TimeInForce = isefix.TimeInForce.GTC
            restingOrders.append(copy.deepcopy(orderAddMsg))
            yield orderAddMsg
        while True:
            for orderAddMsg in restingOrders:
                if orderAddMsg.RequestHeader.MsgType == isefix.MsgType.NewOrderSingle:
                    orderModMsg = orderModSingleMsg
                else:
                    orderModMsg = orderModMultilegMsg
                for field in orderAddMsg.meta():
                    if field[0] not in ('RequestHeader', 'RefOrderID', 'RefClOrdID'):
                        setattr(orderModMsg, field[0], getattr(orderAddMsg, field[0]))
                orderModMsg.OrigClOrdID = orderAddMsg.ClOrdID
                orderModMsg.ClOrdID = orderAddMsg.ClOrdID
                product = [p for p in self.orderAddGen.products if p.id == int(orderAddMsg.MarketSegmentID)][0]
                instrument = int(orderAddMsg.SecurityID)
                orderModMsg._Price = floatToInteger1E8(product.nextPrice(instrument)[0])
                yield orderModMsg


class QuoteDeleteAllFactory(FeedStep):

    def __init__(self, products):
        self.products = products
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, 1), (isefix.Side.Sell, 1)])

        quoteActionMsg = isefix.DeleteAllQuotesRequest()
        quoteActionMsg.RequestHeader.MsgType = isefix.MsgType.QuoteActionRequest
        quoteActionMsg.MassActionScope = isefix.MassActionScope.Market_Segment
        quoteActionMsg.MassActionType = isefix.MassActionType.Cancel

        while True:
            product = random.choice(self.products)
            quoteActionMsg.Side = sideGen.next()
            quoteActionMsg.MarketSegmentID = str(product.id)
            yield quoteActionMsg


class QuoteActionFactory(FeedStep):

    def __init__(self, products, suspend=False, suspendAndCancel=False, release=False):
        self.products = products
        self.suspend = suspend
        self.suspendAndCancel = suspendAndCancel
        self.release = release
        if suspendAndCancel and release:
            raise ValueError("Invalid loadmix configuration! INACTIVATE_AND_CANCEL_QUOTES and REACTIVATEQUOTES are mutually exclusive.")

        FeedStep.__init__(self, None)

    def messageGenerator(self):
        quoteActionMsg = isefix.QuoteActionRequest()
        quoteActionMsg.RequestHeader.MsgType = isefix.MsgType.QuoteActionRequest
        quoteActionMsg.MassActionScope = isefix.MassActionScope.Market_Segment

        quoteActionMsg.TargetMarketSegmentGrp.append(isefix.TargetMarketSegmentGrpComp())

        while True:
            product = random.choice(self.products)

            quoteActionMsg.TargetMarketSegmentGrp[0].TargetMarketSegmentID = str(product.id)

            if self.suspendAndCancel:
                quoteActionMsg.MassActionType = isefix.MassActionType.SuspendAndCancel
                yield quoteActionMsg
            else:
                if self.suspend:
                    quoteActionMsg.MassActionType = isefix.MassActionType.Suspend
                    yield quoteActionMsg

                if self.release:
                    quoteActionMsg.MassActionType = isefix.MassActionType.Release
                    yield quoteActionMsg


class OrderDeleteAllFactory(FeedStep):

    def __init__(self, products, scope="Market_Segment", productID=None, instrumentID=None, markets=None, partitions=None):
        self.products = products
        self.scope = scope
        self.productID = productID
        self.instrumentID = instrumentID
        self.markets = markets
        self.partitions = partitions
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        deleteAllOrdersMsg = isefix.DeleteAllOrdersRequest()
        deleteAllOrdersMsg.RequestHeader.MsgType = isefix.MsgType.OrderMassActionRequest
        deleteAllOrdersMsg.MassActionType = isefix.MassActionType.Cancel
        while True:
            if isefix.MassActionScope._keys_[self.scope][0] == isefix.MassActionScope.Market_Segment:
                deleteAllOrdersMsg.MassActionScope = isefix.MassActionScope.Market_Segment
                deleteAllOrdersMsg.TargetMarketSegmentGrp = []
                deleteAllOrdersMsg.TargetMarketSegmentGrp.append(isefix.TargetMarketSegmentGrpComp())
                if self.productID is None:
                    product = random.choice(self.products)
                    deleteAllOrdersMsg.TargetMarketSegmentGrp[0].TargetMarketSegmentID = str(product.id)
                else:
                    deleteAllOrdersMsg.TargetMarketSegmentGrp[0].TargetMarketSegmentID = str(self.productID)
            elif isefix.MassActionScope._keys_[self.scope][0] == isefix.MassActionScope.Security:
                deleteAllOrdersMsg.MassActionScope = isefix.MassActionScope.Security
                deleteAllOrdersMsg.TargetMarketSegmentGrp = []
                deleteAllOrdersMsg.TargetMarketSegmentGrp.append(isefix.TargetMarketSegmentGrpComp())
                if self.productID is None or self.instrumentID is None:
                    product = random.choice(self.products)
                    instrument = product.getRandomInstrument()
                    deleteAllOrdersMsg.TargetMarketSegmentGrp[0].TargetMarketSegmentID = str(product.id)
                    deleteAllOrdersMsg.SecurityID = str(instrument)
                else:
                    deleteAllOrdersMsg.TargetMarketSegmentGrp[0].TargetMarketSegmentID = str(self.productID)
                    deleteAllOrdersMsg.SecurityID = str(self.instrumentID)
            elif isefix.MassActionScope._keys_[self.scope][0] == isefix.MassActionScope.Market:
                deleteAllOrdersMsg.MassActionScope = isefix.MassActionScope.Market
                if self.markets is None:
                    deleteAllOrdersMsg.MarketID = "XISX"
                else:
                    deleteAllOrdersMsg.MarketID = random.choice(self.markets)
            elif isefix.MassActionScope._keys_[self.scope][0] == isefix.MassActionScope.Partition:
                deleteAllOrdersMsg.MassActionScope = isefix.MassActionScope.Partition
                deleteAllOrdersMsg.PartitionGrp = []
                deleteAllOrdersMsg.PartitionGrp.append(isefix.PartitionGrpComp())
                if self.partitions is None:
                    deleteAllOrdersMsg.PartitionGrp[0].PartitionID = 1
                else:
                    deleteAllOrdersMsg.PartitionGrp[0].PartitionID = random.choice(self.partitions)
            yield deleteAllOrdersMsg


class AddComplexInstrumentFactory(FeedStep):

    def __init__(self, products, stockLegIDs, numLegs=3, complexType=isefix.ProductComplex.Standard_combination, commonLeg=True, buyRatio=1, sellRatio=1):
        self.products = products
        self.products = [product for product in products if len(product.instruments) >= numLegs]
        self.stockLegIDs = stockLegIDs
        if complexType == isefix.ProductComplex.Stock_combination:
            self.products = [product for product in self.products if self.stockLegIDs[product.id]]
        self.complexType = complexType
        self.numLegs = numLegs
        self.commonLeg = commonLeg
        self.buyRatio = buyRatio
        self.sellRatio = sellRatio
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        #TODO: ratios
        #TODO: deliverables
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, self.buyRatio), (isefix.Side.Sell, self.sellRatio)])
        addComplexMsg = isefix.AddComplexInstrumentRequest()
        addComplexMsg.RequestHeader.MsgType = isefix.MsgType.SecurityDefinitionRequest
        addComplexMsg.ProductComplex = self.complexType
        while True:
            product = random.choice(self.products)
            addComplexMsg.MarketSegmentID = str(product.id)
            addComplexMsg.InstrumentLegGrp = []
            if self.commonLeg:
                randomInstruments = product.instruments[0:1] + random.sample(product.instruments[1:], self.numLegs - 1)
            else:
                randomInstruments = random.sample(product.instruments, self.numLegs)
            for instrument in randomInstruments:
                leg = isefix.InstrumentLegGrpComp()
                leg.LegSecurityID = str(instrument)
                leg.LegSide = sideGen.next()
                leg.LegRatioQty = 1
                leg.LegSecurityType = isefix.LegSecurityType.Option
                addComplexMsg.InstrumentLegGrp.append(leg)
            if addComplexMsg.ProductComplex == isefix.ProductComplex.Stock_combination:
                addComplexMsg.InstrumentLegGrp[0].LegSecurityID = str(self.stockLegIDs[product.id])
                addComplexMsg.InstrumentLegGrp[0].LegSecurityType = isefix.LegSecurityType.Common_Stock
            yield addComplexMsg


class ModelGTCOrderAddFactory(FeedStep):

    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, buyPrice, sellPrice, quantity):
        self.products = products
        self.sessionConfig = sessionConfig
        self.clOrdIdGen = clOrdIdGen
        self.mmUnits = mmUnits
        self.buyPrice = buyPrice
        self.sellPrice = sellPrice
        self.quantity = quantity
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        sideGen = self.randomSequenceGenerator([(isefix.Side.Buy, 1), (isefix.Side.Sell, 1)])
        orderAddMsg = isefix.NewOrderSingle()
        orderAddMsg.RequestHeader.MsgType = isefix.MsgType.NewOrderSingle
        orderAddMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderAddMsg.OrdType = isefix.OrdType.Limit
        orderAddMsg.PositionEffect = isefix.PositionEffect.Open
        orderAddMsg.TransactTime = common.datetime.now()
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties.append(isefix.PartiesComp())
        orderAddMsg.Parties[0].PartyRole = isefix.PartyRole.Executing_Unit
        orderAddMsg.Parties[1].PartyRole = isefix.PartyRole.Executing_Trader

        buID = self.sessionConfig.memberID
        user = self.sessionConfig.userID

        while True:
            product = random.choice(self.products)
            instrument = product.getRandomInstrument()
            if buID in self.mmUnits:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Market_Maker
            else:
                orderAddMsg.ClearingCapacity = isefix.ClearingCapacity.Customer
                orderAddMsg.OrderCapacity = isefix.OrderCapacity.Customer
            orderAddMsg.ClOrdID = self.clOrdIdGen.next()
            orderAddMsg.Parties[0].PartyID = str(buID)
            orderAddMsg.Parties[1].PartyID = str(user)
            orderAddMsg.Side = sideGen.next()
            orderAddMsg.TimeInForce = isefix.TimeInForce.GTC
            orderAddMsg.MarketSegmentID = str(product.id)
            orderAddMsg.SecurityID = str(instrument)
            if orderAddMsg.Side == isefix.Side.Buy:
                orderAddMsg._Price = floatToInteger1E8(self.buyPrice)
            else:
                orderAddMsg._Price = floatToInteger1E8(self.sellPrice)
            orderAddMsg.OrderQty = self.quantity
            ModelGTCFactory.restingOrders.append( (orderAddMsg.ClOrdID, orderAddMsg.MarketSegmentID, orderAddMsg.SecurityID, orderAddMsg.Side, orderAddMsg._Price, orderAddMsg.OrderQty) )
            yield orderAddMsg


class ModelGTCOrderDelFactory(FeedStep):

    def __init__(self, products, clOrdIdGen, mmUnits):
        self.products = products
        self.mmUnits = mmUnits
        self.clOrdIdGen = clOrdIdGen
        FeedStep.__init__(self, None)

    def messageGenerator(self):

        orderDelMsg = isefix.OrderCancelRequest()
        orderDelMsg.RequestHeader.MsgType = isefix.MsgType.OrderCancelRequest
        while True:
            while len(ModelGTCFactory.restingOrders) == 0:
                yield None
            restingOrder = ModelGTCFactory.restingOrders.pop(0)
            orderDelMsg.ClOrdID = self.clOrdIdGen.next()
            orderDelMsg.OrigClOrdID = restingOrder[0]
            orderDelMsg.MarketSegmentID = restingOrder[1]
            orderDelMsg.SecurityID = restingOrder[2]
            yield orderDelMsg


class ModelGTCOrderModFactory(FeedStep):

    def __init__(self, products, clOrdIdGen, mmUnits):
        self.products = products 
        self.mmUnits = mmUnits
        self.clOrdIdGen = clOrdIdGen
        FeedStep.__init__(self, None)

    def messageGenerator(self):

        orderModMsg = isefix.OrderCancelReplaceRequest()
        orderModMsg.RequestHeader.MsgType = isefix.MsgType.OrderCancelReplaceRequest
        orderModMsg.ExecInst = isefix.ExecInst.Reinstate_on_failure
        orderModMsg.OrdType = isefix.OrdType.Limit
        orderModMsg.TimeInForce = isefix.TimeInForce.GTC
        orderModMsg.PositionEffect = isefix.PositionEffect.Open
        orderModMsg.TransactTime = common.datetime.now()

        while True:
            orderModMsg.ClearingCapacity = isefix.ClearingCapacity.Market_Maker
            orderModMsg.OrderCapacity = isefix.OrderCapacity.Market_Maker
            while len(ModelGTCFactory.restingOrders) == 0:
                yield None
            restingOrder = ModelGTCFactory.restingOrders.pop(0)
            orderModMsg.OrigClOrdID = restingOrder[0]
            orderModMsg.ClOrdID = self.clOrdIdGen.next()
            orderModMsg.MarketSegmentID = restingOrder[1]
            orderModMsg.SecurityID = restingOrder[2]
            orderModMsg.Side = restingOrder[3]
            orderModMsg._Price = restingOrder[4]
            orderModMsg.OrderQty = restingOrder[5] * 2
            ModelGTCFactory.restingOrders.append( (orderModMsg.ClOrdID, orderModMsg.MarketSegmentID, orderModMsg.SecurityID, orderModMsg.Side, orderModMsg._Price, orderModMsg.OrderQty) )
            yield orderModMsg


class ModelGTCFactory(FeedStep):

    restingOrders = []
    def __init__(self, products, sessionConfig, clOrdIdGen, mmUnits, buyPrice, sellPrice, quantity, preFill):
        self.products = products
        self.preFill = preFill
        self.modelGTCOrderAdd = ModelGTCOrderAddFactory(products, sessionConfig, clOrdIdGen, mmUnits, buyPrice, sellPrice, quantity)
        self.modelGTCOrderDel = ModelGTCOrderDelFactory(products, clOrdIdGen, mmUnits)
        self.modelGTCOrderMod = ModelGTCOrderModFactory(products, clOrdIdGen, mmUnits)
        self.modelGTCFactory = self.randomSequenceGenerator([(self.modelGTCOrderAdd, 1),
                                                            (self.modelGTCOrderDel, 1),
                                                            (self.modelGTCOrderMod, 1)])
        FeedStep.__init__(self, None)

    def messageGenerator(self):
        for i in xrange(self.preFill):
            message = self.modelGTCOrderAdd.nextMessage()
            yield message
        while True:
            message = self.modelGTCFactory.next().nextMessage()
            if message:
                yield message

class ConfigurationException(Exception):
    pass


class Config(object):
    '''  
    Class that provides attribute access to the key-values of a dictionary
    '''
    def __init__(self, configDict):
        self.configDict = configDict

    def __getattr__(self, name):
        if name in self.configDict:
            return self.configDict[name]
        else:
            raise AttributeError("No such attribute: " + name)

class SessionMessagesFactory(object):

    '''
    Per session messages factory.
    '''

    def __init__(self, conf, sessionConfig, refdata, productsMap):
        '''
        :param conf: instance of Config class containing the configuration variables
        defined in the loadmix file
        :param sessionConfig: instance of GatewaySessionConfig
        :param refdata: instance of refdata.ReferenceData
        :param productsMap: map of name of product type to a list of instances of
        perftest.Product class.
        '''
        self.refdata = refdata
        self.sessionConfig = sessionConfig
        self.msgSeqNum = self._initialMsgSeqNum()
        self._msgFactoryMap = self._createMsgFactoryMap(conf, productsMap)

    @classmethod
    def createSessionFactories(cls, conf, sessionsConfigs, refdata, productsMap):
        '''
        Method that initializes the shared msg factories as well as the generators for randomly choosing
        next transactions and instruments to use.
        '''
        cls._transactionTypeGen = FeedStep.randomSequenceGenerator(conf.TRANSACTION_TYPE_RATIO.items(), cacheSize=100000)
        cls.clOrderIdGen = cls._createClOrderIdGen(sessionsConfigs)
        cls._initCommonMsgFactory(conf, refdata, productsMap)

        sessionMsgFactories = [
            SessionMessagesFactory(conf, sessionConfig, refdata, productsMap)
            for sessionConfig in sessionsConfigs
        ]
        return sessionMsgFactories

    @classmethod
    def _createClOrderIdGen(cls, sessionConfigs):
        '''
        Create a generator for the clOrderId. It will be global for all the sessions
        '''
        hashVal = 0
        for sessionConfig in sessionConfigs:
            hashVal = 101 * hashVal + sessionConfig.id
        uniquePrefix = hashVal % 1000
        return FeedStep.clOrderIdGenerator(prefix=uniquePrefix)


    @classmethod
    def _initCommonMsgFactory(cls, conf, refdata, productsMap):

        '''
        Initialize common factories that will be used globally for all the sessions of
        this feeder.
        '''

        msgFactory = {}
        quoteProducts = productsMap['Quote']
        orderProducts = productsMap['Order']
        complexInstrumentsStd = productsMap['Complex Std']
        complexInstrumentsStk = productsMap['Complex Stk']

        mmUnits = refdata.fetchMMUnitsProducts()
        instrumentLegs = refdata.fetchComplexInstrumentsLegs()
        stockLegIDs = refdata.fetchStockLegIDs()

        msgFactory['OrderAdd'] = cls._createOrderAddFactory(conf, orderProducts, mmUnits)
        msgFactory['OrderAddComplexStd'] = cls._createOrderAddFactory(conf, complexInstrumentsStd, mmUnits, instrumentLegs=instrumentLegs)
        msgFactory['OrderAddComplexStock'] = cls._createOrderAddFactory(conf, complexInstrumentsStk, mmUnits, instrumentLegs=instrumentLegs)
        msgFactory['Trades'] = TradesFactory(orderProducts, None, cls.clOrderIdGen, mmUnits, conf.TRADES_PRICE, dayOrders=conf.ORDERADD_DAYORDERS)
        msgFactory['MassQuotes'] = cls._createMassQuoteFactory(conf, quoteProducts)
        msgFactory['MassQuotesComplexStd'] = cls._createMassQuoteFactory(conf, complexInstrumentsStd, isefix.ProductComplex.Standard_combination)
        msgFactory['MassQuotesComplexStock'] = cls._createMassQuoteFactory(conf, complexInstrumentsStk, isefix.ProductComplex.Stock_combination)
        msgFactory['AddComplexInsStd'] = cls._createAddComplexInstFactory(conf, orderProducts, stockLegIDs, isefix.ProductComplex.Standard_combination)
        msgFactory['AddComplexInsStock'] = cls._createAddComplexInstFactory(conf, orderProducts, stockLegIDs, isefix.ProductComplex.Stock_combination)

        cls._commonMsgFactoryMap = msgFactory 

    @classmethod
    def _createOrderAddFactory(cls, conf, products, mmUnits, instrumentLegs=None):
        return OrderAddFactory(products, None, cls.clOrderIdGen, mmUnits, conf.ORDERADD_IOCPERCENT,
                conf.QUANTITY_MIN, conf.QUANTITY_MAX, conf.QUANTITY_STEP, 
                sendOpposite=conf.ORDERADD_SENDCONTRAORDER, buyRatio=conf.ORDERADD_BUYRATIO, 
                sellRatio=conf.ORDERADD_SELLRATIO, persist=conf.ORDERADD_PERSIST,
                allOrNone=conf.ORDERADD_AON, marketPrice=conf.ORDERADD_MARKETPRICE, 
                dayOrders=conf.ORDERADD_DAYORDERS, priceProtectionScope=conf.PRICEPROTECTIONSCOPE, instrumentLegs=instrumentLegs)

    @classmethod
    def _createMassQuoteFactory(cls, conf, products, productComplexType=None):
        return MassQuoteFactory(products, cls.clOrderIdGen, conf.MASSQUOTE_MINSIZE, conf.MASSQUOTE_MAXSIZE,
                conf.QUANTITY_MIN, conf.QUANTITY_MAX, conf.QUANTITY_STEP, conf.MASSQUOTE_SPREAD, 
                productComplexType, conf.MASSQUOTE_SINGLESIDED_PERCENT)

    @classmethod
    def _createAddComplexInstFactory(cls, conf, products, stockLegIDs, productComplexType):
        return AddComplexInstrumentFactory(products, stockLegIDs, 
                numLegs=conf.COMPLEXINSTRUMENTADD_NUMLEGS, complexType=productComplexType,
                commonLeg=conf.COMPLEXINSTRUMENTADD_COMMONLEG, buyRatio=conf.COMPLEXINSTRUMENTADD_BUYRATIO, 
                sellRatio=conf.COMPLEXINSTRUMENTADD_SELLRATIO)

    def nextMessage(self):
        '''
        Returns a tuple (session id, message). The message generated must be
        sent through the gateway session bound to this factory, whose id
        is returned, as the message was generated specificly for this session 
        (user, business unit, message sequence number...)
        '''
        transactionType = self._transactionTypeGen.next()

        msgFactory = self._getMsgFactory(transactionType)
        message = msgFactory.nextMessage()

        # setting MsgSeqNum for cases msgs are to be pregenerated/buffered
        message.RequestHeader.MsgSeqNum = self.msgSeqNum
        self.msgSeqNum += 1

        return (self.sessionConfig.id, message)

    def _getMsgFactory(self, transactionType):
        #XXX: although orderAdd and Trades generators are common generators for all sessions,
        # they still need the current session config at the moment of generating the message. 
        if transactionType in ['OrderAdd', 'OrderAddComplexStd', 'OrderAddComplexStock', 'Trades'] :
            msgFactory = self._msgFactoryMap[transactionType]
            msgFactory.setCurrentSessionConfig(self.sessionConfig)

        return self._msgFactoryMap[transactionType]


    def _createMsgFactoryMap(self, conf, productsMap):

        '''
        Create a map of message factories for this particular instance tied to a single
        session. It combines global factories reused for all sessions and factories
        only used by this session.
        '''

        msgFactory = dict(SessionMessagesFactory._commonMsgFactoryMap)

        quoteProducts = productsMap['Quote']
        orderProducts = productsMap['Order']
        complexInstrumentsStd = productsMap['Complex Std']
        complexInstrumentsStk = productsMap['Complex Stk']

        mmUnits = self.refdata.fetchMMUnitsProducts()
        instrumentLegs = self.refdata.fetchComplexInstrumentsLegs()
        stockLegIDs = self.refdata.fetchStockLegIDs()

        msgFactory['OrderAddDel'] = self._createOrderAddDelFactory(conf, orderProducts, mmUnits)
        msgFactory['OrderAddMod'] = self._createOrderAddModFactory(conf, orderProducts, mmUnits)
        orderAdd = msgFactory['OrderAdd']
        orderAdd.setCurrentSessionConfig(self.sessionConfig)
        orderAddComplexStd = msgFactory['OrderAddComplexStd']
        orderAddComplexStd.setCurrentSessionConfig(self.sessionConfig)
        msgFactory['OrderAddModResting'] = self._createAddModRestingFactory(conf, orderAdd)
        msgFactory['OrderAddModRestingStd'] = self._createAddModRestingFactory(conf, orderAddComplexStd)

        msgFactory['DeleteAllOrders'] = OrderDeleteAllFactory(orderProducts, markets=conf.MARKETS, partitions=conf.PARTITIONS, scope=conf.DELETEALLORDERS_SCOPE)
        msgFactory['CancelQuotes'] = QuoteDeleteAllFactory(quoteProducts)
        msgFactory['QuoteAction'] = QuoteActionFactory(quoteProducts, suspend=True, suspendAndCancel=conf.INACTIVATE_AND_CANCEL_QUOTES, release=conf.REACTIVATEQUOTES)

        msgFactory['ModelGTC'] = self._createModelGTCFactory(conf, orderProducts, mmUnits)
        return msgFactory


    def _initialMsgSeqNum(self):
        '''
        Returns the first message sequence number that can be used.
        '''
        # The first message is for gw logon
        msgSeqNum = 2 
        # If broacasts subscriptions are enabled, add 1 per each one and market
        if self.sessionConfig.broadcast:
            msgSeqNum += len(GatewaySession.BROADCASTS_APPL_IDS) * len(self.sessionConfig.markets)
        return msgSeqNum


    def _createOrderAddDelFactory(self, conf, products, mmUnits):
        return OrderAddDelFactory(products, self.sessionConfig, self.clOrderIdGen, mmUnits, conf.ORDERDEL_SEQUENCELEN, 
                conf.QUANTITY_MIN, conf.QUANTITY_MAX, conf.QUANTITY_STEP, dayOrders=conf.ORDERADD_DAYORDERS)

    def _createOrderAddModFactory(self, conf, products, mmUnits):
        return OrderAddModFactory(products, self.sessionConfig, self.clOrderIdGen, mmUnits, conf.ORDERMOD_SEQUENCELEN, 
                conf.QUANTITY_MIN, conf.QUANTITY_MAX, conf.QUANTITY_STEP, dayOrders=conf.ORDERADD_DAYORDERS)

    def _createAddModRestingFactory(self, conf, orderAddFactory):
        return OrderAddModRestingFactory(orderAddFactory, conf.ORDERMOD_RESTING_NUMORDERS,
                dayOrders=conf.ORDERADD_DAYORDERS)
        
    def _createModelGTCFactory(self, conf, products, mmUnits):
        return ModelGTCFactory(products, self.sessionConfig, self.clOrderIdGen, mmUnits,
                buyPrice=conf.MODELGTC_BUYPRICE, sellPrice=conf.MODELGTC_SELLPRICE,
                quantity=conf.MODELGTC_QUANTITY, preFill=conf.MODELGTC_PREFILL)



class FileBufferMessagesFactory(object):
    '''  
    Messages factory that reads already encoded messages from a file.
    '''

    def __init__(self, msgFile, gwSessionsConfigs):
        self.generator = self._msgGenerator(msgFile)
        self.gwSessionConfigsGen = itertools.cycle(gwSessionsConfigs)

    def _msgGenerator(self, msgFile):
        with open(msgFile, "r") as f:
            for encodingMethod, msgBuffer in MsgIO.fileToBuf(f):
                yield msgBuffer

    def nextMessage(self):
        return (self.gwSessionConfigsGen.next().id, self.generator.next())



class LoadMix(FeedStep):

    def __init__(self, loadmixConfig=None, rdaConfig=None, gwConfig=None, broadcast=False, 
            inFile=None, pullOrders=False, pullOrdersNP=False, pullQuotes=False):

        self.statsLock = Lock()
        self.latencyHist = Histogram()
        self.msgSent = 0
        self.successResponses = 0
        self.rejectResponses = 0
        self.rejectDetails = {}
        self.orderBcast = 0
        self.quoteBcast = 0
        self.dealItemBcast = 0
        self.tradeItemBcast = 0
        self.mmExecutionBcast = 0
        self.mmBcast = 0
        MARKETS = None
        PARTITIONS = None
        TRADES_PERCENT = 0
        TRADES_PRICE = 60
        PRICEPROTECTIONSCOPE = None
        ORDERADD_SENDCONTRAORDER = False
        ORDERADD_BUYRATIO = 1
        ORDERADD_SELLRATIO = 1
        ORDERADD_PERSIST = True
        ORDERADD_AON = False
        ORDERADD_MARKETPRICE = False
        ORDERADD_DAYORDERS = False
        MASSQUOTE_SPREAD = None
        MASSQUOTE_SINGLESIDED_PERCENT = 0
        INACTIVATE_AND_CANCEL_QUOTES = False
        COMPLEXINSTRUMENTADDSTANDARD_PERCENT = 0
        COMPLEXINSTRUMENTADDSTOCK_PERCENT = 0
        COMPLEXINSTRUMENTADD_NUMLEGS = 2
        COMPLEXINSTRUMENTADD_COMMONLEG = False
        DELETEALLORDERS_PERCENT = 0
        DELETEALLORDERS_SCOPE = "Market_Segment"
        MODELGTC_PERCENT = 0
        MODELGTC_BUYPRICE = 60
        MODELGTC_SELLPRICE = 60.01
        MODELGTC_QUANTITY = 100
        MODELGTC_PREFILL = 0
        ORDERADD_COMPLEXSTD_PERCENT = 0
        ORDERADD_COMPLEXSTK_PERCENT = 0
        MASSQUOTE_COMPLEXSTD_PERCENT = 0
        MASSQUOTE_COMPLEXSTK_PERCENT = 0
        ORDERMOD_RESTING_PERCENT = 0
        ORDERMOD_RESTING_COMPLEXSTD_PERCENT = 0
        ORDERMOD_RESTING_NUMORDERS = 1
        COMPLEX_INSTRUMENTS_STANDARD = {}
        COMPLEX_INSTRUMENTS_STOCK = {}
        COMPLEXINSTRUMENTADD_BUYRATIO = 1
        COMPLEXINSTRUMENTADD_SELLRATIO = 1
        RDA_DBHOST = "127.0.0.1"
        RDA_DBPORT = 10000 + int(os.environ["GTSENV"]) * 100
        RDA_DBUSER = RDA_DBPASSWORD = RDA_DBNAME = "viewdb"
        GATEWAY_SESSIONS = []
        GATEWAY_HOST = "127.0.0.1"
        GATEWAY_PORT = 10006 + int(os.environ["GTSENV"]) * 100
        GATEWAY_MEMBERNAME = "ABG_MM"
        GATEWAY_USERNAME = "ABG_MM_U1"
        GATEWAY_PASSWORD = "PASSWORD"
        CONNECTION_GATEWAY = False
        MARKETS_BROADCAST = [None]

        if rdaConfig:
            config = self.getConfig(rdaConfig)
            exec(config)
        else:
            config = self.getLazyConfig("rda.cfg")
            exec(config)
            RDA_DBHOST = "127.0.0.1"
            RDA_DBPORT = 10000 + int(os.environ["GTSENV"]) * 100

        refdata = ReferenceData(dbhost=RDA_DBHOST, dbport=RDA_DBPORT, 
                                dbuser=RDA_DBUSER, dbpasswd=RDA_DBPASSWORD, 
                                dbname=RDA_DBNAME)

        if gwConfig:
            config = self.getConfig(gwConfig)
            exec(config)
        else:
            config = self.getLazyConfig("gateway.cfg")
            exec(config)
            GATEWAY_HOST = "127.0.0.1"
            GATEWAY_PORT = 10006 + int(os.environ["GTSENV"]) * 100

        self.gatewayMembername = GATEWAY_MEMBERNAME
        self.gatewayUsername = GATEWAY_USERNAME
        self.gatewayHost = GATEWAY_HOST
        self.gatewayPort = GATEWAY_PORT

        if loadmixConfig:
            config = self.getConfig(loadmixConfig)
            exec(config)
        else:
            config = self.getLazyConfig("loadmix.cfg")
            exec(config)


        if not 'TRANSACTION_TYPE_RATIO' in locals():
            TRANSACTION_TYPE_RATIO = {
                'OrderAdd': ORDERADD_PERCENT,
                'OrderAddComplexStd': ORDERADD_COMPLEXSTD_PERCENT,
                'OrderAddComplexStock': ORDERADD_COMPLEXSTK_PERCENT,
                'OrderAddDel': ORDERDEL_PERCENT,
                'OrderAddMod': ORDERMOD_PERCENT,
                'OrderAddModResting': ORDERMOD_RESTING_PERCENT,
                'OrderAddModRestingStd': ORDERMOD_RESTING_COMPLEXSTD_PERCENT,
                'MassQuotes': MASSQUOTE_PERCENT,
                'MassQuotesComplexStd': MASSQUOTE_COMPLEXSTD_PERCENT,
                'MassQuotesComplexStock': MASSQUOTE_COMPLEXSTK_PERCENT,
                'Trades': TRADES_PERCENT,
                'DeleteAllOrders': DELETEALLORDERS_PERCENT,
                'CancelQuotes': CANCELQUOTES_PERCENT,
                'QuoteAction': INACTIVATEQUOTES_PERCENT,
                'AddComplexInsStd': COMPLEXINSTRUMENTADDSTANDARD_PERCENT,
                'AddComplexInsStock': COMPLEXINSTRUMENTADDSTOCK_PERCENT,
                'ModelGTC': MODELGTC_PERCENT,
            }



        priceStepTables = refdata.fetchPriceStepTables()
        for key, value in priceStepTables.iteritems():
            Product.cachedPrices[key] = Product.generateRandomUpAndDownPrices(PRICE_INIT, PRICE_MIN, PRICE_MAX, PRICE_CHANGE_TICKS, PRICE_CHANGE_FREQUENCY, PRICE_CHANGEDIR_FREQUENCY, value)

        orderProducts = []
        for key, value in ORDER_PRODUCTS_INSTRUMENTS.items():
            product = Product(key, PRODUCTS_PRICESTEPS[key])
            for id in value:
                product.addInstrument(id)
            orderProducts.append(product)

        quoteProducts = []
        for key, value in QUOTE_PRODUCTS_INSTRUMENTS.items():
            product = Product(key, PRODUCTS_PRICESTEPS[key])
            for id in value:
                product.addInstrument(id)
            quoteProducts.append(product)

        complexInstrumentsStd = []
        for key, value in COMPLEX_INSTRUMENTS_STANDARD.items():
            product = Product(key, PRODUCTS_PRICESTEPS[key])
            for id in value:
                product.addInstrument(id)
            complexInstrumentsStd.append(product)

        complexInstrumentsStk = []
        for key, value in COMPLEX_INSTRUMENTS_STOCK.items():
            product = Product(key, PRODUCTS_PRICESTEPS[key])
            for id in value:
                product.addInstrument(id)
            complexInstrumentsStk.append(product)


        self.gatewayMembername = GATEWAY_MEMBERNAME
        self.gatewayUsername = GATEWAY_USERNAME
        try:
            self.gatewayHost = GATEWAY_HOST
            self.gatewayPort = GATEWAY_PORT
        except:
            self.gatewayHost = None
            self.gatewayPort = None

        productsMap = {
            'Order': orderProducts, 
            'Quote': quoteProducts, 
            'Complex Std': complexInstrumentsStd, 
            'Complex Stk': complexInstrumentsStk, 
        }
        config = Config(locals())
        self.gwSessions = self._createGWSessions(config, refdata, broadcast, pullOrders, pullOrdersNP, pullQuotes)

        if inFile:
            self.sessionMsgFactories = [FileBufferMessagesFactory(inFile, self.gwSessions)]
        else:
            self.sessionMsgFactories = SessionMessagesFactory.createSessionFactories(config, self.gwSessions, refdata, productsMap)

        FeedStep.__init__(self, None)

    def getLazyConfig(self, fileName):
        '''
        Get the configuration from the specified filename located in LAZY_CFG_DIR
        '''
        return self.getConfig(os.path.join(LAZY_CFG_DIR, fileName))

    def getConfig(self, configFile):
        f = open(configFile, "r")
        data = f.read()
        f.close()
        return data

    def messageGenerator(self):
        while True:
            for sessionMsgFactory in self.sessionMsgFactories:
                yield sessionMsgFactory.nextMessage()


    def responseHandler(self, responseMsg):
        self.statsLock.acquire()
        if isinstance(responseMsg, isefix.PrivateOrderBroadcast):
            self.orderBcast += 1
        elif isinstance(responseMsg, isefix.PrivateQuoteBroadcast):
            self.quoteBcast += 1
        elif isinstance(responseMsg, isefix.DealItemBroadcast):
            self.dealItemBcast += 1
        elif isinstance(responseMsg, isefix.TradeItemBroadcast):
            self.tradeItemBcast += 1
        elif isinstance(responseMsg, isefix.PrivateMMExecutionReportBroadcast):
            self.mmExecutionBcast += 1
        elif isinstance(responseMsg, isefix.PrivateMarketMakerBroadcast):
            self.mmBcast += 1

        if isinstance(responseMsg, SUCESS_MSG_TYPES) or \
                isinstance(responseMsg, SUCESS_MSG_TYPES_PRODUCT) and responseMsg.MarketSegmentID is None:
            self.successResponses += 1
            if options.verbose > 1:
                print responseMsg
        elif isinstance(responseMsg, isefix.Reject):
            self.rejectResponses += 1
            self.rejectDetails[responseMsg.Text] = self.rejectDetails.get(responseMsg.Text, 0) + 1
            if options.verbose > 0:
                print responseMsg
        else:
            self.statsLock.release()
            if options.verbose > 1:
                print responseMsg
            return
        reqTime = responseMsg.ResponseHeader._RequestTime
        sndTime = responseMsg.ResponseHeader._SendingTime
        if reqTime and sndTime:
            timeDelta = int((sndTime - reqTime)/1E3)
            self.latencyHist.put(timeDelta)
        self.statsLock.release()

    def requestHandler(self, requestMsg):
        self.statsLock.acquire()
        self.msgSent += 1
        self.statsLock.release()
        if options.verbose > 2:
            print requestMsg

    def statsGenerator(self, tstamp):
        self.statsLock.acquire()
        self.latencyHist.update_cumbins()
        try:
            cumbins = self.latencyHist.cumbins.dumps()
        except:
            cumbins = numpy.dumps(self.latencyHist.cumbins)        
        stats = {
            "time" : tstamp,
            "total_sent" : self.msgSent,
            "total_samples" : self.latencyHist.total_samples,
            "total_value" : self.latencyHist.total_value,
            "min_value" : self.latencyHist.min_value,
            "max_value" : self.latencyHist.max_value,
            "bin_number" : self.latencyHist.bin_number,
            "bin_width" : self.latencyHist.bin_width,
            "bin_above" : self.latencyHist.bin_above,
            "cumbins" : cumbins,
            "successful" : self.successResponses,
            "rejected" : self.rejectResponses,
            "rejected_details" : self.rejectDetails,
        }
        self.latencyHist.reset()
        self.msgSent = 0
        self.successResponses = 0
        self.rejectResponses = 0
        self.rejectDetails = {}
        self.orderBcast = 0
        self.quoteBcast = 0
        self.dealItemBcast = 0
        self.tradeItemBcast = 0
        self.mmExecutionBcast = 0
        self.mmBcast = 0
        self.statsLock.release()
        return stats

    def _createGWSessions(self, config, refdata, broadcast, pullOrders, 
            pullOrdersNP, pullQuotes):

        def configFromDict(confdict):
            try:
                memberName = confdict["memberName"]
                userName = confdict["userName"]
                password = confdict["password"]
                host = confdict["gwHost"]
                port = int(confdict["gwPort"])
                connGateway = confdict["connGateway"] == "True"

            except Exception as e:
                raise ConfigurationException("Invalid gateway configuration: %s" %
                        confdict, e), None, sys.exc_info()[2]

            buUserIDs = refdata.fetchBUUserIDs(memberName, userName)
            if not buUserIDs:
                raise ConfigurationException("Unable to get business unit and user id from login name %s" % userName)
            buID, userID = buUserIDs

            return GatewaySessionConfig(buID, memberName, userID, userName, 
                    password, host, port, connGateway=connGateway, 
                    pullOrdersNP=pullOrdersNP, pullOrders=pullOrders, pullQuotes=pullQuotes, 
                    broadcast=broadcast, markets=config.MARKETS_BROADCAST)


        if not config.GATEWAY_SESSIONS:
            config.GATEWAY_SESSIONS = [{
                "memberName": config.GATEWAY_MEMBERNAME,
                "userName": config.GATEWAY_USERNAME,
                "password": config.GATEWAY_PASSWORD,
                "gwHost": config.GATEWAY_HOST,
                "gwPort": config.GATEWAY_PORT,
                "connGateway": config.CONNECTION_GATEWAY,
            }]
        print 'GATEWAY_SESSIONS'
        print config.GATEWAY_SESSIONS
        return [configFromDict(conf) for conf in config.GATEWAY_SESSIONS]


if __name__ == "__main__":

    parser = OptionParser()
    parser.add_option("-g", "--gw-config", dest="gwConfig", help="Gateway configuration file", default = None)
    parser.add_option("-H", "--gw-host", dest="gwHost", help="Hostname of the machine running the gateway", default = None)
    parser.add_option("-p", "--gw-port", dest="gwPort", type="int", help="TCP Port for the gateway", default = None)
    parser.add_option("-l", "--loadmix-config", dest="loadmixConfig", help="Load mix configuration file")
    parser.add_option("-i", "--initial-rate", dest="iniRate", type="int", help="Initial feeding rate", default = 1)
    parser.add_option("-f", "--final-rate", dest="finRate", type="int", help="Final feeding rate", default = 1)
    parser.add_option("-s", "--step-rate", dest="stepRate", type="int", help="Step increment for feeding rate", default = 1)
    parser.add_option("-t", "--duration", dest="duration", type="int", help="Duration of each test run (in seconds)", default = 10)
    parser.add_option("-c", "--controller-config", dest="controllerConfig", help="Master controller configuration file", default = None)
    parser.add_option("-r", "--rda-config", dest="rdaConfig", help="Reference data access configuration file", default = None)
    parser.add_option("-m", "--input-file", dest="inFile", help="Read pre-encoded messages from file", default = None)
    parser.add_option("-o", "--output-file", dest="outFile", help="Dump encoded messages to file", default = None)
    parser.add_option("-n", "--num-messages", dest="outNumber", type="int", help="Number of messages to be dumped to file", default = 1)
    parser.add_option("-b", "--broadcast", dest="broadcast", action="store_true", help="Subscribe to private broadcasts", default = False)
    parser.add_option("-d", "--mddConfig", dest="mddConfig", help="Track mdd broadcast messages", default = None)
    parser.add_option("-v", "--verbose", dest="verbose", type="int", help="Verbosity level", default = 0)
    parser.add_option("-u", "--burst-len", dest="burstLen", type="int", help="Burst length", default = 1)
    parser.add_option("--pattern", dest="pattern", help="Special rate pattern", default = "steadyRate()")
    parser.add_option("--pull-orders", dest="pullOrders", action="store_true", help="Pull all orders on disconnect", default = False)
    parser.add_option("--pull-orders-np", dest="pullOrdersNP", action="store_true", help="Pull non-persistent orders on disconnect", default = False)
    parser.add_option("--pull-quotes", dest="pullQuotes", action="store_true", help="Pull quotes on disconnect", default = False)

    (options, args) = parser.parse_args()
    if options.finRate < options.iniRate:
        options.finRate = options.iniRate

    loadMix = LoadMix(options.loadmixConfig, options.rdaConfig, options.gwConfig,
            broadcast=options.broadcast, inFile=options.inFile, 
            pullOrders=options.pullOrders, pullOrdersNP=options.pullOrdersNP, pullQuotes=options.pullQuotes)

    if options.gwHost and options.gwPort:
        gwHost = options.gwHost
        gwPort = options.gwPort
    else:
        gwHost = loadMix.gatewayHost
        gwPort = loadMix.gatewayPort

    perfTest = IsefixPerformanceTest(loadMix.gwSessions)

    perfTest.registerResponseHandler(loadMix.responseHandler)
    perfTest.registerRequestHandler(loadMix.requestHandler)
    perfTest.registerStatsGenerator(loadMix.statsGenerator)

    perfTest.addFeedSequence([loadMix], repetitions=0)

    if options.controllerConfig:
        perfTest.masterController(options.controllerConfig)
        sys.exit(0)

    if options.outFile:
        perfTest.pregenerate(options.outFile, options.outNumber)
        print "Dumped %s messages to %s" % (options.outNumber, options.outFile)
        sys.exit(0)

    if options.mddConfig:
        mddTracker = MDDMessageTracker(options.mddConfig)

    for rate in xrange(options.iniRate, options.finRate + options.stepRate, options.stepRate):
        perfTest.run(sendRate=rate, duration=options.duration, burstLen=options.burstLen, pattern=options.pattern)
        header = "msgSent,successResponses,rejectResponses,min,avg,50p,90p,99p"
        values = "%d,%d,%d,%d,%d,%d,%d,%d" % \
                    (loadMix.msgSent, loadMix.successResponses, loadMix.rejectResponses, \
                    loadMix.latencyHist.min_value, loadMix.latencyHist.mean(), \
                    loadMix.latencyHist.percentile(50), loadMix.latencyHist.percentile(90), \
                    loadMix.latencyHist.percentile(99))

        if options.broadcast:
            header += ",orderBcast,quoteBcast,dealItemBcast,tradeItemBcast,mmExecutionBcast,mmBcast"
            values += ",%d,%d,%d,%d,%d,%d" % \
                    (loadMix.orderBcast, loadMix.quoteBcast, loadMix.dealItemBcast, \
                    loadMix.tradeItemBcast, loadMix.mmExecutionBcast, loadMix.mmBcast)

        if options.mddConfig:
            header += ",topOfBookQuote,topOfBookTicker,depthIncremental,orderOnBook"
            values += ",%d,%d,%d,%d" % \
                    (mddTracker.topOfBookQuote, mddTracker.topOfBookTicker, \
                    mddTracker.depthIncremental, mddTracker.orderOnBook)

        print header
        print values
    perfTest.shutdown()
