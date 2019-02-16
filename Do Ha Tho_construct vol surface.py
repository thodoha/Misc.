# -*- coding: utf-8 -*-
"""
Created on Thu Dec 28 16:36:45 2017

@author: Tho Do
"""

import ib_insync
import pandas as pd
import datetime as dt
import numpy as np
from dateutil import relativedelta
import pyodbc
import matplotlib.pyplot as plt
from matplotlib import cm
from mpl_toolkits.mplot3d import Axes3D
import xlwings as xw
# =============================================================================
# USER DEFINED FUNCTIONS
def validate_date(date):
    '''
    This function checks whether a date in string format is Friday (monthly contract)
    Input: date in string format yyyymmdd
    Output: dates if satisfying the criteria
    '''
    y = int(date[0:4])
    m = int(date[4:6])
    d = int(date[6:8])
    dd = dt.date(y,m,d)
    
    if dd.month == (dt.date.today() + relativedelta.relativedelta(months = 1)).month and dd.day // 7 + 1 == 4:
        pass
    else:
        return dd

# specifying contract ID of the underlying
def req_underlying(ib,conId):
    '''
    This method requests the contract details of the underlying of the surface
    Input: ib object, contract IB ID
    Output: contract object
    '''
    underlying = ib_insync.ib.Contract(conId = conId)
    ib.qualifyContracts(underlying)
    print(underlying)
    return underlying


def req_underlying_price(ib,underlying):
    '''
    This method requests prior close price of the underlying. This will later be used
    to calculate what strikes should we get to construct vol surface
    Input: ib object,underlying contract object
    Output: prior close price of the underlying
    '''
    underlying_close = ib.reqHistoricalData(underlying, endDateTime = '', durationStr = '1 D',
                            barSizeSetting = '1 day', whatToShow = 'TRADES',
                            useRTH = True, formatDate = 1)
    underlying_close = ib_insync.util.df(underlying_close)
    underlying_close = underlying_close.close[len(underlying_close)-1]
    print('req_undelrying_price')
    return underlying_close

def req_opt_chain(ib, underlying, opt_exchange):
    '''
    This method requests the option chains and filter it down to desired exchange
    Input: ib object, underlying contract obj, name of exchange
    Output: a dataframe of option chain with info on strikes and expiraitons
    '''
    chains  = ib.reqSecDefOptParams(underlying.symbol, opt_exchange, underlying.secType, underlying.conId)
    
    print('req_opt_chain')
    return chains

def req_strikes_and_expirations(ib, chains, underlying_close):
    '''
    This method extracts strikes and expirations that are suitable to construct vol surface
    Strikes: +/- 15% from last close price
    Expirations: all monthly tenors up to 6 months from today,
    weekly tenors as well but only right before the first monthly tenor if today is on the fourth 
    week of the month. Otherwise grab all weekly tenors.
    The reason is because sometimes the weekly options after the first monthly option from today
    do not have enough strikes
    Input: ib connection obj, chains_df, prior close of underlying
    Output: a list of strikes and a list of expirations (yyyymmdd format)
    '''
    expirations = set()
    strikes = set()
    for i in range(len(chains)):
        expirations.update(chains[i].expirations)
        strikes.update(chains[i].strikes)
    strikes = list(strikes)
    strikes = sorted([s for s in strikes if s.is_integer() and underlying_close* 0.75 < s < underlying_close*1.25])
    expirations = list(expirations)
    expirations = [e for e in expirations if  dt.date.today() < ib_insync.util.parseIBDatetime(e) < dt.date.today() + dt.timedelta(days = 365)]

    print('req_strikes_and_exp')
    return strikes, expirations

def req_options(ib, underlying, underlying_close, strikes,expirations,option_exchange):
    '''
    This method requests options implied vol by pairing strike and expiration
    from the list and create a dataframe for it.
    Since IB doesn't allow us to pull more than 100 securities at once, we split
    the entire option universe into a batch of 100 and concatenate later.
    Input: ib connection obj, strikes, expiraitons, exchange
    Output contract dataframe
    '''
    number_of_contracts = len(strikes) * len(expirations)
    contracts = list()
    for expiration in expirations:
        for strike in strikes:
            if strike < underlying_close:
                contracts.append(ib_insync.contract.Contract(symbol = underlying.symbol,
                                                             secType = "FOP",
                                                             lastTradeDateOrContractMonth=expiration,
                                                             strike = strike,
                                                             right = "P",
                                                             exchange = option_exchange))
            elif strike >= underlying_close:
                contracts.append(ib_insync.contract.Contract(symbol=underlying.symbol,
                                             secType = "FOP",
                                           lastTradeDateOrContractMonth=expiration,
                                           strike=strike,
                                           right="C",
                                           exchange=option_exchange))
        
    contracts_df = pd.DataFrame()
    
    if number_of_contracts <= 100:
        contracts_df = ib_insync.util.df(ib.reqTickers(*contracts))
    else:
        number_of_batches = number_of_contracts // 100 + 1
        for i in range(number_of_batches):
            print("Retrieve batch number {}.".format(i+1))
            contracts_sub = list()
            contracts_sub = contracts[100*i:100*(i+1)]
            contracts_sub_df = ib_insync.util.df(ib.reqTickers(*contracts_sub))
            ib.sleep(1)
            contracts_df = pd.concat([contracts_df, contracts_sub_df],axis = 0)
            print("===============================================================")
    
    contracts_df.reset_index(drop = True, inplace = True)
    contracts_df = contracts_df['contract close bidGreeks askGreeks lastGreeks modelGreeks'.split()]
    contracts_df["strikes"] = [c.strike for c in contracts_df.contract]
    contracts_df["expirations"] = [ib_insync.util.parseIBDatetime(c.lastTradeDateOrContractMonth) for c in contracts_df.contract]
    contracts_df["impliedVol"] = [c.impliedVol  if c is not None else np.nan for c in contracts_df.modelGreeks]
    
    contracts_df['bidIV'] = [c.impliedVol  if c is not None else np.nan for c in contracts_df.bidGreeks]        
    contracts_df['askIV'] = [c.impliedVol  if c is not None else np.nan for c in contracts_df.askGreeks]
    
    contracts_df['finalIV'] = [0.5*(contracts_df.loc[i,'bidIV']+contracts_df.loc[i,'askIV']) if not np.isnan(contracts_df.loc[i,'bidIV']) else contracts_df.loc[i,'askIV'] for i in contracts_df.index]                
    
    print("reqOptions")
    return contracts_df

def construct_options_matrix(contracts_df,strikes):
    '''
    This method is going to filter out all combinations of strikes and expirations
    and leaves us with a dataframe with rows are strikes and columns are expirations
    Input: contracts_df
    Output: a dataframe of matrix
    '''
    prelim_expirations = list(set(contracts_df.expirations.tolist()))
    contracts_matrix_dict = dict()
    for e in prelim_expirations:
        contracts_matrix_dict[e] = contracts_df[contracts_df.expirations == e].impliedVol.tolist()
    
    contracts_matrix = pd.DataFrame(data = contracts_matrix_dict, index = strikes)
    contracts_matrix.dropna(axis = 1, how = 'all', inplace = True)
    contracts_matrix.dropna(axis = 0, how = 'any', inplace = True)
    
    print("construct matrix")
    return contracts_matrix

# =============================================================================
# COLLECTING OPTION CHAIN DATA AND CONSTRUCT VOL SURFACE
# connecting to IB API


conn_str = (r'DRIVER={Microsoft Access Driver (*.mdb, *.accdb)};'
            r'DBQ=...\VolSurface.accdb;')
    
# create aconnection ADB, more on this can be found on pyodbc documentation
cnxn = pyodbc.connect(conn_str)
crsr = cnxn.cursor()

# name of the database
underlying_db = 'DUMMY'

# SQL query to pull IB contract ID from the database
crsr.execute("""SELECT tblSecurity.IBContractID
             FROM tblSecurity
             WHERE (((tblSecurity.MandateID)=?));
             """,underlying_db)
underlying_id = crsr.fetchone().IBContractID

# connect to IB API
ib = ib_insync.ib.IB()
ib.connect('127.0.0.1', 7496, clientId=13)

# switch to frozen data if there is no live data
ib.reqMarketDataType(2)


underlying = req_underlying(ib,underlying_id)

# request historical price of previous day from IB

underlying_close = req_underlying_price(ib,underlying) 


# request option chain
# exchange of the option
option_exchange = 'GLOBEX'
chains = req_opt_chain(ib, underlying, option_exchange)

## extract strikes and expirations
strikes, expirations = req_strikes_and_expirations(ib, chains, underlying_close)

# request option data
contracts_df = req_options(ib, underlying, underlying_close, strikes, expirations,option_exchange)

# disconnect to the IB API
ib.disconnect()

# create an option matrix with rows as strikes and columns as expirations
contracts_matrix = construct_options_matrix(contracts_df,strikes)

matrix_strikes = sorted(contracts_matrix.index.tolist())
matrix_expirations = sorted(contracts_matrix.columns.tolist())

#### =============================================================================================
#### EXPORT TO DATABASE
print("update to database")

# remove existing data in the database
crsr.execute("""DELETE FROM tblStrikes WHERE (((tblStrikes.MandateID) = ?));
             """, underlying_db)
crsr.commit()

crsr.execute("""DELETE FROM tblExpirations WHERE (((tblExpirations.MandateID) = ?))
             """, underlying_db)

crsr.commit()

crsr.execute("""DELETE FROM tblImpliedVols WHERE (((tblImpliedVols.MandateID) = ?))
             """, underlying_db)

time = dt.datetime.now()

# update tblStrikes in the database
for strike in matrix_strikes:
    crsr.execute("""INSERT INTO tblStrikes (MandateID, Strike, UpdateTime)
                        VALUES (?,?,?)
                     """, underlying_db, strike, time)
    crsr.commit()

# update expirations and implied vol in the database
for e in matrix_expirations:
    if e <= ib_insync.util.parseIBDatetime(underlying.lastTradeDateOrContractMonth):
        crsr.execute("""INSERT INTO tblExpirations (MandateID, SecurityID, Expiration, UpdateTime)
                    VALUES (?,?,?,?)
                    """, underlying_db, underlying.localSymbol, e, time)
        crsr.commit()
        for strike in matrix_strikes:
            crsr.execute("""INSERT INTO tblImpliedVols (MandateID, SecurityID, Strike, Expiration, ImpliedVol, UpdateTime)
                            VALUES (?,?,?,?,?,?)
                         """, underlying_db, underlying.localSymbol ,strike, e, contracts_matrix.loc[strike,e],time)
            crsr.commit()
    else:
        crsr.execute("""INSERT INTO tblExpirations (MandateID, SecurityID, Expiration, UpdateTime)
                    VALUES (?,?,?,?)
                    """, underlying_db, underlying_2.localSymbol, e, time)
        crsr.commit()
    
        for strike in matrix_strikes:
            crsr.execute("""INSERT INTO tblImpliedVols (MandateID, SecurityID, Strike, Expiration, ImpliedVol, UpdateTime)
                            VALUES (?,?,?,?,?,?)
                         """, underlying_db, underlying_2.localSymbol, strike, e, contracts_matrix.loc[strike,e],time)
            crsr.commit()
