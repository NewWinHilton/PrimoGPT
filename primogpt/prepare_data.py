import os
import re
import csv
import math
import time
import json
import finnhub
from tqdm import tqdm
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, date
from datetime import time as dt_time
from collections import defaultdict
import datasets
from datasets import Dataset
from openai import OpenAI
from dotenv import load_dotenv

# Setting up environment variables
load_dotenv()
finnhub_key = os.environ.get("FINNHUB_KEY")
openai_key = os.environ.get("OPENAI_KEY")

finnhub_client = finnhub.Client(api_key=finnhub_key)
client = OpenAI(api_key=openai_key)

# Mapping stock returns to one of the U/D classes
def bin_mapping(ret):

    up_down = 'U' if ret >= 0 else 'D'

    integer = math.ceil(abs(100 * ret))
    
    return up_down + (str(integer) if integer <= 5 else '5+')

# Downloading stock data and creating all necessary columns
def get_returns(stock_symbol, start_date, end_date):

    stock_data = yf.download(stock_symbol, start=start_date, end=end_date, auto_adjust=False)
    
    # If we have multi-level columns, flatten them
    if isinstance(stock_data.columns, pd.MultiIndex):
        stock_data.columns = [col[0] for col in stock_data.columns]
    
    # Calculate returns
    returns = stock_data['Adj Close'].pct_change()
    
    # Ensure all input data is one-dimensional
    dates = stock_data.index.strftime('%Y-%m-%d').tolist()
    prices = stock_data['Adj Close'].values.flatten()  # Ensures 1D
    returns_values = returns.values.flatten()  # Ensures 1D
    
    data = pd.DataFrame({
        'Date': dates,
        'Adj Close Price': prices,
        'Returns': returns_values
    }).dropna()

    data['Bin Label'] = data['Returns'].apply(bin_mapping)

    return data

def is_valid_timestamp(timestamp):
    """Validate if a timestamp is valid (not 0 or None and within reasonable range)."""
    try:
        if not timestamp or timestamp <= 0:
            return False
        # Check if timestamp is within reasonable range (1970 to 2050)
        dt = datetime.fromtimestamp(timestamp)
        return 1970 <= dt.year <= 2050
    except (ValueError, TypeError, OSError):
        return False

# Downloading news about the stock
def get_news(symbol, data):
    news_list = []

    # List of sources for filtering
    valid_sources = ["Fintel", "InvestorPlace", "Seeking Alpha", "SeekingAlpha", "Yahoo", "CNBC", "TipRanks", "MarketWatch", "The Fly", "Benzinga", "TalkMarkets", "Stock Options Channel"]

    for index in range(len(data)):
        # Checking if it's the last row
        if index < len(data) - 1:
            current_row = data.iloc[index]
            next_row = data.iloc[index + 1]
        else:
            current_row = data.iloc[index]
            next_row = current_row

        if isinstance(current_row['Date'], str):
            current_date = datetime.strptime(current_row['Date'], '%Y-%m-%d')
        else:
            current_date = current_row['Date']

        if isinstance(next_row['Date'], str):
            next_day_date = datetime.strptime(next_row['Date'], '%Y-%m-%d')
        else:
            next_day_date = next_row['Date']

        start_date_str = current_date.strftime('%Y-%m-%d')
        next_day_date_str = next_day_date.strftime('%Y-%m-%d')

        # Due to API limit
        time.sleep(0.2)

        # Defining market closed hours
        start_time = dt_time(16, 00)
        end_time = dt_time(9, 30)

        try:
            # Fetching news
            news_items = finnhub_client.company_news(symbol, _from=start_date_str, to=next_day_date_str)

            # Transform news items with validation
            transformed_news = []
            for n in news_items:
                try:
                    if not is_valid_timestamp(n.get('datetime')):
                        print(f"Skipping news item with invalid timestamp: {n.get('datetime')}")
                        continue
                    
                    transformed_news.append({
                        "date": datetime.fromtimestamp(n['datetime']),
                        "headline": n['headline'],
                        "summary": n['summary'],
                        "source": n['source'],
                    })
                except (KeyError, ValueError) as e:
                    print(f"Error processing news item: {str(e)}")
                    continue

            # Filtering news by time outside market hours and by source
            filtered_news = [
                {
                    "date": news['date'].strftime('%Y%m%d%H%M%S'),
                    "headline": news['headline'],
                    "summary": news['summary'],
                    "source": news['source'],
                }
                for news in transformed_news
                if (
                    (news['date'].date() == current_date.date() and news['date'].time() >= start_time) or
                    (current_date.date() < news['date'].date() < next_day_date.date()) or
                    (news['date'].date() == next_day_date.date() and news['date'].time() <= end_time)
                ) and news['source'] in valid_sources
            ]

            filtered_news.sort(key=lambda news: news['date'])
            news_list.append(json.dumps(filtered_news))

        except Exception as e:
            print(f"Error fetching news for {symbol} on {start_date_str}: {str(e)}")
            news_list.append(json.dumps([]))  # Add empty list for this date

    data['News'] = news_list
    return data

def get_press_releases(symbol, data):

    press_releases_list = []

    for index in range(len(data)):
        current_row = data.iloc[index]
        current_date = pd.to_datetime(current_row['Date'])
        
        if index < len(data) - 1:
            next_row = data.iloc[index + 1]
            next_day_date = pd.to_datetime(next_row['Date'])
        else:
            # For the last day, we use the next day as the end of the period
            next_day_date = current_date + pd.Timedelta(days=1)

        start_date_str = current_date.strftime('%Y-%m-%d')
        next_day_date_str = next_day_date.strftime('%Y-%m-%d')

        time.sleep(0.4)  # Due to API limit


        response = finnhub_client.press_releases(symbol, _from=start_date_str, to=next_day_date_str)
        
        transformed_releases = [
            {
                "date": pr['datetime'],
                "headline": pr['headline'],
                "description": pr['description']
            }
            for pr in response.get('majorDevelopment', [])
        ]
        
        transformed_releases.sort(key=lambda pr: pr['date'])


        press_releases_list.append(json.dumps(transformed_releases))

    data['PressReleases'] = press_releases_list
    return data

# Filter only what we need
def get_company_profile(symbol):
    profile = finnhub_client.company_profile(symbol=symbol)
    
    # Filtering only required data
    filtered_profile = {
        'name': profile.get('name'),
        'exchange': profile.get('exchange'),
        'marketCapitalization': profile.get('marketCapitalization'),
        'employeeTotal': profile.get('employeeTotal'),
        'industry': profile.get('finnhubIndustry'),
        'symbol': profile.get('ticker') 
    }
    
    return filtered_profile

# Main function for downloading data
def prepare_data_for_symbol(symbol, data_dir, start_date, end_date):
    data = get_returns(symbol, start_date, end_date)
    print("Returns done")

    data = get_news(symbol, data)
    print("News done")

    data = get_press_releases(symbol, data)
    print("Press releases done")

    filename = f"{symbol}_{start_date}_{end_date}.csv"

    data.to_csv(os.path.join(data_dir, filename), index=False)
    
    return data