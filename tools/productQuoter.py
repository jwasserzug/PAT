#!/usr/bin/python
from startOfDay import *

class ProductQuoter(StartOfDay):

    def __init__(self, bidPrice=60, offerPrice=None, businessDate=None,
                    partitions=None, gwHost=None, gwPort=None, ofiHost=None,
                    ofiPort=None, options=None):
        """ """
        refdata = ReferenceData(dbhost=options.dbhost, dbport=options.dbport)
        self.options = options
        self.refdata = refdata
        self.units = refdata.fetchUnitsUsers()
        self.products = refdata.fetchProductsInstruments(partitions=partitions, closingPositionOnly=False)
        self.securities = refdata.fetchSecurities()
        self.pmmunits = refdata.fetchPMMUnitsProducts()
        self.mmusers  = refdata.fetchMMUserNameProducts()
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

    def responseHandler(self, msg):
        if self.options.verbose:
            print msg
    
    def requestHandler(self, msg):
        if self.options.verbose:
            print msg


    def quote_products(self, login_name, bid_price, offer_price, bid_size,
                        offer_size, productId=None):
        """ Sends quotes for a single partition for a single user """
        # Mass quotes from PMM users
        memberName, userName = login_name.split('-')
        password = self.passwords.get(login_name, "PASSWORD")
        buUserIDs = self.refdata.fetchBUUserIDs(memberName, userName)
        if not buUserIDs:
            raise Exception("Unable to get business unit and user id from login name %s" % login_name)
        buID, userID = buUserIDs
        gwSessConfig = GatewaySessionConfig(buID, memberName, userID, userName, password,
                self.gwHost, self.gwPort, connGateway=options.conngateway)
        gwClient = ThrottledGatewaySession(gwSessConfig)
        gwClient.responseHandler = self.responseHandler
        gwClient.requestHandler = self.requestHandler
        gwClient.connect()
        if not gwClient.logonRejected:
            gwClient.sendMessages(self.massQuote(login_name, bid_price, offer_price, bid_size, offer_size, productId))
            print("Quotes for %s sent" % login_name)
        else:
            print("Logon for %s was rejected: %s" % (login_name, gwClient.logonRejectedReason))
        
        time.sleep(0.5) #TODO: gwsession doesn't properly end session
        gwClient.disconnect()
        
        print("Quotes from CMM user sent")

    def massQuote(self, loginName, bid_price, offer_price, bid_size, offer_size, productId=None):
        """ """
        #max_quote_size = 100
        massQuoteMsg = isefix.MassQuote()
        massQuoteMsg.RequestHeader.MsgType = isefix.MsgType.MassQuote
        count = 1

        print self.mmusers.keys()

        if productId:
            if productId in self.mmusers[loginName]:
                product_list = [productId]
            else:
                product_list = []
                print("ProductID %s does not belong to %s" % (productId, loginName))
        else:
            product_list = self.mmusers[loginName]

        print("Quoting %s Products..." % len(product_list))
        for productId in product_list:
            instrument_list = self.products.get(productId, [])
            print("Quoting %s instruments for product %s" % 
                    (len(instrument_list), productId))

            massQuoteMsg.QuoteID = "MQ_OPEN_" + str(count)
            massQuoteMsg.QuotSetGrp = [isefix.QuotSetGrpComp()]
            massQuoteMsg.QuotSetGrp[0].MarketSegmentID = str(productId)

            for instrumentId in instrument_list:
#                print 'Quoting %s' % instrumentId
                quotEntryGrp = isefix.QuotEntryGrpComp()
                quotEntryGrp.SecurityID = str(instrumentId)
                quotEntryGrp._BidPx = int(bid_price * 1E8)
                quotEntryGrp.BidSize = bid_size
                quotEntryGrp._OfferPx = int(offer_price * 1E8)
                quotEntryGrp.OfferSize = offer_size
                massQuoteMsg.QuotSetGrp[0].QuotEntryGrp.append(quotEntryGrp)
                if len(massQuoteMsg.QuotSetGrp[0].QuotEntryGrp) >= bid_size:
                    yield massQuoteMsg
                    count += 1
                    massQuoteMsg.QuotSetGrp[0].QuotEntryGrp=[]
            if massQuoteMsg.QuotSetGrp[0].QuotEntryGrp:
                yield massQuoteMsg
                count += 1

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
    parser.add_option("--bbo-offer-price", dest="offerPrice", type="float", default=None, help="Best offer price")
    parser.add_option("--bbo-bid-price", dest="bidPrice", type="float", default=60, help="Best bid price")
    parser.add_option("--business-date", dest="businessDate", type="str", default=None, help="Set current business date (dd-mm-yyyy)")
    parser.add_option("--show-business-dates", dest="showBusinessDates", action="store_true", default=False, help="Show business dates in the database")
    parser.add_option("--passwords", dest="passwords", help="Passwords file for GW and OFI", default = None)
    parser.add_option("--conngateway", dest="conngateway", action="store_true", help="Use this flag when using a connection gateway", default = False)
    parser.add_option("--gwlogin", dest="login", type="str", default=None, help="Gateway login name")
    parser.add_option("--bidsize", dest="bidSize", type="int", default=10, help="Bid size")
    parser.add_option("--offersize", dest="offerSize", type="int", default=10, help="Offer size")
    parser.add_option("--productid", dest="productID", type="int", default=None, help="Product ID")
    

    (options, args) = parser.parse_args()
    
    if options.showBusinessDates:
        refdata = ReferenceData(dbhost=options.dbhost, dbport=options.dbport)
        refdata.showBusinessDates()
        sys.exit(0)
    
    if options.partition:
        options.partition = [options.partition]
    
    productQuoter = ProductQuoter(
                        bidPrice     =  options.bidPrice,
                        offerPrice   =  options.offerPrice,
                        businessDate =  options.businessDate,
                        partitions   =  options.partition,
                        gwHost       =  options.gwhost,
                        gwPort       =  options.gwport,
                        ofiHost      =  options.ofihost,
                        ofiPort      =  options.ofiport,
                        options      =  options
                    )

    productQuoter.quote_products(
            login_name  = options.login,
            bid_price   = options.bidPrice,
            offer_price = options.offerPrice,
            bid_size    = options.bidSize,
            offer_size  = options.offerSize,
            productId   = options.productID
    )

    productQuoter.printResults()

#    startOfDay.run()
#    startOfDay.printResults()
#    if startOfDay.successful > 0:
#        sys.exit(0)
#    else:
#        sys.exit(1)
