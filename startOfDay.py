#!/usr/bin/python

import math
import os, sys
import random
from optparse import OptionParser
from decimal import Decimal
import random
import datetime
import time

from testfeeders.perftest import GatewaySession, OFIPerformanceTest, Product, GatewaySessionConfig
from testfeeders.refdata import ReferenceData
from OSCAR import Messages, MsgIO, isefix
from testfeeders import commonbasic

matcher = Messages.matcher
outboundfi = Messages.outboundfi
common = Messages.common


class ThrottledGatewaySession(GatewaySession):

    LOGIN_TIMEOUT_SECS = 10


    def __init__(self, config, msgFile=None):
        GatewaySession.__init__(self, config, msgFile=msgFile)
        self.loggedIn = False
        self.logonRejected = False
        self.logonRejectedReason = ""
        self.rate = None 


    def onMessage(self, responseMsg):
        GatewaySession.onMessage(self, responseMsg)
        if responseMsg.ID == isefix.LogonResp.ID:
            self._setThrottlingRate(responseMsg)
            self.loggedIn = True
        
        if responseMsg.ID == isefix.Reject.ID and responseMsg.ResponseHeader.MsgSeqNum == 1:
            self.logonRejected = True
            self.logonRejectedReason = responseMsg.Text


    def login(self):
        GatewaySession.login(self)
        st_time = time.time()
        timedOut = False
        while not self.loggedIn and not self.logonRejected and not timedOut:
            time.sleep(0.1)
            timedOut = (time.time() - st_time) > self.LOGIN_TIMEOUT_SECS

        if timedOut:
            self.logonRejected = True
            self.logonRejectedReason = "No response received in %s" % self.LOGIN_TIMEOUT_SECS


    def _setThrottlingRate(self, logonRespMsg):
        throttleParamsGrp = self._getInboundRateThrottle(logonRespMsg)
        if throttleParamsGrp:
            noMsgs = throttleParamsGrp.ThrottleNoMsgs
            timeInterval = throttleParamsGrp.ThrottleTimeInterval
            timeUnit = throttleParamsGrp.ThrottleTimeUnit
            #self.rate = noMsgs / (timeInterval / math.pow(10, timeUnit)) -1
            #print("Setting sending rate to %s msgs/sec" % self.rate)


    def _getInboundRateThrottle(self, logonRespMsg):
        if logonRespMsg.ThrottleParamsGrp:
            for throttleParamsGrp in logonRespMsg.ThrottleParamsGrp:
                if throttleParamsGrp.ThrottleType == isefix.ThrottleType.Inbound_Rate:
                    return throttleParamsGrp


    def sendMessages(self, msgGenerator):
        freq = 0 
        if self.rate > 0:
            freq = 1 / float(self.rate)
            for msg in msgGenerator:
                next_ts = time.time() + freq
                self.sendMsg(msg)
                delay = next_ts - time.time()
                if delay > 0:
                    time.sleep(delay)
        else:
            for msg in msgGenerator:
                self.sendMsg(msg)


class StartOfDay:

    def __init__(self, bidPrice=60, offerPrice=None, businessDate=None, partitions=None, gwHost=None, gwPort=None, ofiHost=None, ofiPort=None):
        refdata = ReferenceData(dbhost=options.dbhost, dbport=options.dbport)
        self.refdata = refdata
        self.units = refdata.fetchUnitsUsers()
        self.products = refdata.fetchProductsInstruments(partitions=partitions, closingPositionOnly=False)
        self.securities = refdata.fetchSecurities()
        self.pmmunits = refdata.fetchPMMUnitsProducts()
        self.pmmusers = refdata.fetchPMMUserNameProducts()
        self.houseunits = refdata.fetchHouseUnitsUsers()
        self.mopsuser = refdata.fetchMOPSUserName()
        self.priceSteps = refdata.fetchPriceSteps()
        self.businessDate = businessDate
        self.cachedPrices = {}
        for key, value in refdata.fetchPriceStepTables().iteritems():
            if offerPrice == None:
                self.cachedPrices[key] = Product.generateRandomUpAndDownPrices(60, 60, 100, 1, 0, 0, value, size=1)
            else:
                self.cachedPrices[key] = [(bidPrice, offerPrice)*2]
        self.attempted = 0
        self.successful = 0
        self.unsuccessful = []
        self.gwHost = gwHost
        self.gwPort = gwPort
        self.ofiHost = ofiHost
        self.ofiPort = ofiPort
        self.passwords = {}
        if options.passwords:       
            f = open(options.passwords, "r")
            for line in f.readlines():
                line = line.rstrip("\r\n")
                loginName, password = line.split(",")
                self.passwords[loginName] = password
            f.close()

    def changeProductState(self, state):
        changeProductStateMsgOFI = outboundfi.OFIChangeProductStateMopsRequestMessage()
        changeProductStateMsgOFI.reqHdr.tranType = common.TransactionType.CHANGE_PRODUCT_STATE_MOPS_REQUEST
        changeProductStateMsg = matcher.ChangeProductStateMopsRequestMessage()
        changeProductStateMsg.appHeader.transactionType = common.TransactionType.CHANGE_PRODUCT_STATE_MOPS_REQUEST
        changeProductStateMsg.appHeader.enteringBuID = self.houseunits.keys()[0]
        changeProductStateMsg.appHeader.enteringUserID = self.houseunits.values()[0][0]
        changeProductStateMsg.appHeader.executingBuID = self.houseunits.keys()[0]
        changeProductStateMsg.appHeader.executingUserID = self.houseunits.values()[0][0]
        if self.businessDate:
            changeProductStateMsg.changeProductState.currentBusinessDate = datetime.date.fromtimestamp(time.mktime(time.strptime(self.businessDate, "%d-%m-%Y")))
        else:
            changeProductStateMsg.changeProductState.currentBusinessDate = datetime.date.today()
        changeProductStateMsg.changeProductState.productState = state
        for productId in self.products:
            changeProductStateMsg.changeProductState.productID = productId
            changeProductStateMsgOFI.data = changeProductStateMsg
            yield changeProductStateMsgOFI

    def modifyProductMarketMakerParameter(self, buID):
        mmparamdef = outboundfi.OFIModifyMarketMakerParameterMopsRequestMessage()
        mmparamdef.reqHdr.tranType = common.TransactionType.MODIFY_MARKET_MAKER_PARAMETER_MOPS_REQUEST
        mmparamdef.data.mmParameter.tickWorseTicks=3
        mmparamdef.data.mmParameter.tickWorseQty=100
        mmparamdef.data.mmParameter.mmpVolumeLimit=100000000
        mmparamdef.data.mmParameter.mmpDeltaLimit=100000000
        mmparamdef.data.mmParameter.mmpVegaLimit=100000000
        mmparamdef.data.mmParameter.mmpPercentLimit=100000000
        mmparamdef.data.mmParameter.mmpTimeWindow=1000
        mmparamdef.data.mmParameter.instrumentType=Messages.basic.InstrumentType.CLEARING_INSTRUMENT_OPTION
        mmparamdef.data.mmParameter.mmRole=Messages.basic.MarketMakerRole.PMM
        mmparamdef.data.mmParameter.buID=buID
        yield mmparamdef


    def underlyingBBO(self):
        underlyingBBOMsgOFI = outboundfi.OFIUnderlyingBBOUpdateRequestMessage()
        underlyingBBOMsgOFI.reqHdr.tranType = common.TransactionType.UNDERLYING_BBO_UPDATE_REQUEST
        underlyingBBOMsg = matcher.UnderlyingBBOUpdateRequestMessage()
        underlyingBBOMsg.appHeader.transactionType = common.TransactionType.UNDERLYING_BBO_UPDATE_REQUEST
        underlyingBBOMsg.appHeader.enteringBuID = self.houseunits.keys()[0]
        underlyingBBOMsg.appHeader.enteringUserID = self.houseunits.values()[0][0]
        underlyingBBOMsg.appHeader.executingBuID = self.houseunits.keys()[0]
        underlyingBBOMsg.appHeader.executingUserID = self.houseunits.values()[0][0]
        for productId in self.products:
            underlyingBBOMsg.appHeader.productID = productId
            underlyingBBOMsg.underlyingBBO.productID = productId
            underlyingBBOMsg.underlyingBBO.securityID = self.securities[productId]
            underlyingBBOMsg.underlyingBBO.underlyingStatus = commonbasic.UnderlyingStatus.REGULAR_TRADING
            prices = self.cachedPrices[self.priceSteps[productId]][0]
            underlyingBBOMsgOFI.data = underlyingBBOMsg
            yield underlyingBBOMsgOFI
            # Send one update for status and another one for prices
            underlyingBBOMsg.underlyingBBO.underlyingStatus = None
            underlyingBBOMsg.underlyingBBO.bidPrice = Decimal(str(prices[0]))
            underlyingBBOMsg.underlyingBBO.offerPrice = Decimal(str(prices[1]))
            underlyingBBOMsgOFI.data = underlyingBBOMsg
            yield underlyingBBOMsgOFI
 
    def massQuote(self, loginName):
        max_quote_size = 100
        massQuoteMsg = isefix.MassQuote()
        massQuoteMsg.RequestHeader.MsgType = isefix.MsgType.MassQuote
        count = 1
        for productId in self.pmmusers[loginName]:
            prices = self.cachedPrices[self.priceSteps[productId]][0]
            massQuoteMsg.QuoteID = "MQ_OPEN_" + str(count)
            massQuoteMsg.QuotSetGrp = [isefix.QuotSetGrpComp()]
            massQuoteMsg.QuotSetGrp[0].MarketSegmentID = str(productId)
            for instrumentId in self.products.get(productId, []):
                quotEntryGrp = isefix.QuotEntryGrpComp()
                quotEntryGrp.SecurityID = str(instrumentId)
                quotEntryGrp._BidPx = int(prices[0] * 1E8)
                quotEntryGrp.BidSize = 100
                quotEntryGrp._OfferPx = int(prices[1] * 1E8)
                quotEntryGrp.OfferSize = 100
                massQuoteMsg.QuotSetGrp[0].QuotEntryGrp.append(quotEntryGrp)
                if len(massQuoteMsg.QuotSetGrp[0].QuotEntryGrp) >= max_quote_size:
                    yield massQuoteMsg
                    count += 1
                    massQuoteMsg.QuotSetGrp[0].QuotEntryGrp=[]
            if massQuoteMsg.QuotSetGrp[0].QuotEntryGrp:
                yield massQuoteMsg
                count += 1

    def changeInstrumentState(self, state):
        changeInstrumentStateMsgOFI = outboundfi.OFIChangeInstrumentStateMopsRequestMessage()
        changeInstrumentStateMsgOFI.reqHdr.tranType = common.TransactionType.CHANGE_INSTRUMENT_STATE_MOPS_REQUEST
        changeInstrumentStateMsg = matcher.ChangeInstrumentStateRequestMessage()
        changeInstrumentStateMsg.appHeader.transactionType = common.TransactionType.CHANGE_INSTRUMENT_STATE_REQUEST
        changeInstrumentStateMsg.changeInstrumentState.targetInstrumentState = state
        for productId in self.products:
            changeInstrumentStateMsg.appHeader.enteringBuID = self.houseunits.keys()[0]
            changeInstrumentStateMsg.appHeader.enteringUserID = self.houseunits.values()[0][0]
            changeInstrumentStateMsg.appHeader.executingBuID = self.houseunits.keys()[0]
            changeInstrumentStateMsg.appHeader.executingUserID = self.houseunits.values()[0][0]
            changeInstrumentStateMsg.changeInstrumentState.productID = productId
            changeInstrumentStateMsg.changeInstrumentState.instrumentType = commonbasic.InstrumentType.CLEARING_INSTRUMENT_OPTION
            changeInstrumentStateMsgOFI.data = changeInstrumentStateMsg
            yield changeInstrumentStateMsgOFI

    def run(self):
        password = self.passwords.get(self.mopsuser, "PASSWORD")
        ofiClient = OFIPerformanceTest(ofiHost=self.ofiHost, ofiPort=self.ofiPort, loginName=self.mopsuser, password=password)
        ofiClient.registerResponseHandler(self.responseHandler)
        ofiClient.registerRequestHandler(self.requestHandler)

        # Change product state: START_OF_DAY->CLOSED->PRE_OPEN->OPEN
        for prodState in (commonbasic.ProductState.START_OF_DAY, commonbasic.ProductState.CLOSED, commonbasic.ProductState.PRE_OPEN, commonbasic.ProductState.OPEN):
            for msg in self.changeProductState(prodState):
                ofiClient.sendMsg(msg)
            time.sleep(5)
        print("Products rotated to OPEN")

        # Underlying BBO
        for msg in self.underlyingBBO():
            ofiClient.sendMsg(msg)
        print("Underlying BBO sent")

        # Change instrument state to OPENING
        for msg in self.changeInstrumentState(commonbasic.InstrumentState.OPENING):
            msg.reqHdr.reqSeqNo = None
            ofiClient.sendMsg(msg)
        time.sleep(10)
        print("Instruments state set to OPENING")

        # Send Modify MarketMakerParameter requests to set PMM limits to be able to quote
        for buId in self.pmmunits.keys():
            for msg in self.modifyProductMarketMakerParameter(buId):
                msg.reqHdr.reqSeqNo = None
                ofiClient.sendMsg(msg)
                time.sleep(1)
        time.sleep(10)
        print("MarketMakerParameter limits sent")

        # Mass quotes from PMM users
        for loginName in self.pmmusers:
            memberName, userName = loginName.split('-')
            password = self.passwords.get(loginName, "PASSWORD")
            buUserIDs = self.refdata.fetchBUUserIDs(memberName, userName)
            if not buUserIDs:
                raise Exception("Unable to get business unit and user id from login name %s" % loginName)
            buID, userID = buUserIDs
            gwSessConfig = GatewaySessionConfig(buID, memberName, userID, userName, password, 
                    self.gwHost, self.gwPort, connGateway=options.conngateway) 
            gwClient = ThrottledGatewaySession(gwSessConfig)
            gwClient.responseHandler = self.responseHandler
            gwClient.requestHandler = self.requestHandler
            gwClient.connect()
            if not gwClient.logonRejected:
                gwClient.sendMessages(self.massQuote(loginName))
                print("Quotes for %s sent" % loginName)
            else:
                print("Logon for %s was rejected: %s" % (loginName, gwClient.logonRejectedReason))
                
            time.sleep(0.5) #TODO: gwsession doesn't properly end session
            gwClient.disconnect()

        print("Quotes from PMM users sent")

        # Change instrument state to REGULAR
        ofiClient.registerResponseHandler(self.changeInstrumentCounter)
        for msg in self.changeInstrumentState(commonbasic.InstrumentState.REGULAR):
            msg.reqHdr.reqSeqNo = None
            ofiClient.sendMsg(msg)
            self.attempted += 1
        time.sleep(10)

    def responseHandler(self, msg):
        if options.verbose:
            print msg

    def requestHandler(self, msg):
        if options.verbose:
            print msg

    def changeInstrumentCounter(self, msg):
        self.responseHandler(msg)
        if isinstance(msg, outboundfi.OFIChangeInstrumentStateMopsResponseMessage):
            if len(msg.data.instrumentStateData) == 0:
                self.successful += 1
            else:
                self.unsuccessful.append(msg.data.instrumentStateData)

    def printResults(self):
        print "%d / %d products rotated all their instruments to REGULAR state." % (self.successful, self.attempted)
        for instrumentStates in self.unsuccessful:
            for instrumentState in instrumentStates:
                print "Product: %s  Instrument: %s  State: %s  Reason: %s" % \
                        (instrumentState.productID, instrumentState.instrumentID, \
                        commonbasic.InstrumentState._vals_[instrumentState.state][0], \
                        commonbasic.StateReason._vals_[instrumentState.reason][0])


if __name__ == "__main__":
    basePort = 10000 + int(os.environ["GTSENV"]) * 100
    parser = OptionParser()
    parser.add_option("-P", "--partition", dest="partition", type="int", default=None, help="Partition number")
    parser.add_option("-v", "--verbose", dest="verbose", action="store_true", default=False, help="Verbose mode")
    parser.add_option("--dbhost", dest="dbhost", default="127.0.0.1", help="Reference data database host")
    parser.add_option("--dbport", dest="dbport", type="int", default=basePort, help="Reference data database port")
    parser.add_option("--gwhost", dest="gwhost", default="127.0.0.1", help="Gateway host")
    parser.add_option("--gwport", dest="gwport", type="int", default=basePort + 6, help="Gateway port")
    parser.add_option("--ofihost", dest="ofihost", default="127.0.0.1", help="OutboundFeedInterface host")
    parser.add_option("--ofiport", dest="ofiport", type="int", default=basePort + 5, help="OutboundFeedInterface port")
    parser.add_option("--bbo-offer-price", dest="offerPrice", type="int", default=None, help="Best offer price")
    parser.add_option("--bbo-bid-price", dest="bidPrice", type="int", default=60, help="Best bid price")
    parser.add_option("--business-date", dest="businessDate", type="str", default=None, help="Set current business date (dd-mm-yyyy)")
    parser.add_option("--show-business-dates", dest="showBusinessDates", action="store_true", default=False, help="Show business dates in the database")
    parser.add_option("--passwords", dest="passwords", help="Passwords file for GW and OFI", default = None)
    parser.add_option("--conngateway", dest="conngateway", action="store_true", help="Use this flag when using a connection gateway", default = False)

    (options, args) = parser.parse_args()

    if options.showBusinessDates:
        refdata = ReferenceData(dbhost=options.dbhost, dbport=options.dbport)
        refdata.showBusinessDates()
        sys.exit(0)

    if options.partition:
        options.partition = [options.partition]

    startOfDay = StartOfDay(bidPrice=options.bidPrice, offerPrice=options.offerPrice, businessDate=options.businessDate, partitions=options.partition, gwHost=options.gwhost, gwPort=options.gwport, ofiHost=options.ofihost, ofiPort=options.ofiport)
    startOfDay.run()
    startOfDay.printResults()
    if startOfDay.successful > 0:
        sys.exit(0)
    else:
        sys.exit(1)
