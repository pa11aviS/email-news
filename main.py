#!/usr/bin/env python3
"""
Daily News Summarizer and Emailer
Fetches news from RSS feeds and NewsAPI, summarizes with Ollama, and emails to Gmail.
"""

import json
import os
import sys
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
import requests
import ollama
import xml.etree.ElementTree as ET
from urllib.request import urlopen
import json
import os
import markdown

def load_config():
    """Load configuration from config.json"""
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("config.json not found. Please create it with your API keys.")
        sys.exit(1)

def fetch_rss_news(rss_urls, days_back=1):
    """Fetch news from RSS feeds"""
    articles = []
    cutoff_date = datetime.now() - timedelta(days=days_back)
    
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                published = entry.get('published_parsed')
                if published:
                    pub_date = datetime(*published[:6])
                    if pub_date >= cutoff_date:
                         articles.append({
                             'title': entry.title,
                             'content': entry.get('summary') or entry.get('description') or '',
                             'url': entry.link,
                             'source': feed.feed.title if hasattr(feed.feed, 'title') else url,
                             'published': pub_date.isoformat()
                         })
        except Exception as e:
            print(f"Error fetching RSS from {url}: {e}")
    
    return articles

def fetch_newsapi_news(api_key, query="world news", days_back=1, sources=None):
    """Fetch news from NewsAPI"""
    articles = []
    from_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    
    if sources:
        url = f"https://newsapi.org/v2/top-headlines?sources={sources}&pageSize=15&apiKey={api_key}"
    else:
        url = f"https://newsapi.org/v2/everything?q={query}&from={from_date}&sortBy=publishedAt&pageSize=15&apiKey={api_key}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        for article in data.get('articles', []):
            articles.append({
                'title': article['title'],
                'content': article.get('content') or article.get('description') or '',
                'url': article['url'],
                'source': article['source']['id'],
                'published': article['publishedAt']
            })
    except Exception as e:
        print(f"Error fetching from NewsAPI: {e}")
    
    return articles

def summarize_news(sections, api_key):
    """Select and format news articles by sections using Ollama"""
    
    # Collect all articles into a numbered list
    all_articles = []
    article_map = {}
    counter = 1
    for section_name, articles in sections.items():
        for art in articles[:10]:  # Limit per section
            all_articles.append(f"{counter}. Title: {art['title']}\n   Content: {art['content'][:200]}\n   Source: {art['source']}")
            article_map[counter] = art
            counter += 1
    
    article_list = "\n\n".join(all_articles)
    
    prompt_select = f"""You are a journalist curating an email newsletter for a highly-educated professional audience.

    Here is a numbered list of articles. For each section, select the numbers of articles that fit the criteria. Select 5-7 per section. Make sure there is relevant content for each section.

    Sections:
    - AI NEWS: Articles about artificial intelligence, machine learning, AI releases.
    - MAJOR INTERNATIONAL NEWS: Global news, politics, world events.
    - AUSTRALIAN NEWS: News from or about Australia.
    - SPORTS NEWS: Cricket, F1, athletics, soccer, running only.
    - TECH NEWS: Technology, gadgets, software.
    - LONG-FORM ARTICLES: In-depth analysis, features.
    - TRENDING ON SOCIAL MEDIA: Viral topics, trends.

    Output ONLY in this format:
    AI NEWS: 1,3,5
    MAJOR INTERNATIONAL NEWS: 2,4
    ...

    No extra text.

    Articles:
    {article_list}
    """
    
    try:
        response = ollama.chat(
            model=api_key,
            messages=[{"role": "user", "content": prompt_select}]
        )
        selection_text = response['message']['content'].strip()
        
        # Parse selections
        selected_articles = {}
        for line in selection_text.split('\n'):
            if ':' in line:
                section, nums = line.split(':', 1)
                section = section.strip().upper()
                nums = [int(n.strip()) for n in nums.split(',') if n.strip().isdigit()]
                selected_articles[section] = [article_map[n] for n in nums if n in article_map]
        
        # Format as HTML
        html = ""
        for section in ['AI NEWS', 'MAJOR INTERNATIONAL NEWS', 'AUSTRALIAN NEWS', 'SPORTS NEWS', 'TECH NEWS', 'LONG-FORM ARTICLES', 'TRENDING ON SOCIAL MEDIA']:
            if section in selected_articles and selected_articles[section]:
                html += f"<h2>{section}</h2><ul>"
                for art in selected_articles[section][:7]:
                    title = art['title']
                    summary = art['content'][:300]  # Use existing summary
                    link = art['url']
                    source = art['source']
                    html += f"<li><strong>{title}</strong>: {summary} <a href='{link}'>[{source}]</a></li>"
                html += "</ul>"
        
        return html
    except Exception as e:
        print(f"Error with Ollama selection: {e}")
        return "<h1>Error generating summary</h1>"

def get_weather(config):
    """Get weather forecast for Eleebana NSW from BOM (today and tomorrow)"""
    fallback_file = 'weather_fallback.json'
    
    # Load previous fallback
    fallback = {}
    if os.path.exists(fallback_file):
        try:
            with open(fallback_file, 'r') as f:
                fallback = json.load(f)
        except:
            pass
    
    try:
        url = "ftp://ftp.bom.gov.au/anon/gen/fwo/IDN11051.xml"
        with urlopen(url) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        
        # Find Newcastle area
        for area in root.findall('.//area'):
            if area.get('description') == 'Newcastle' and area.get('type') == 'location':
                forecasts = []
                tomorrow_min = tomorrow_max = None
                for index in ["0", "1"]:  # Today and tomorrow
                    forecast = area.find(f'forecast-period[@index="{index}"]')
                    if forecast is not None:
                        min_temp_elem = forecast.find('element[@type="air_temperature_minimum"]')
                        max_temp_elem = forecast.find('element[@type="air_temperature_maximum"]')
                        precip_elem = forecast.find('element[@type="precipitation_range"]')
                        prob_precip_elem = forecast.find('text[@type="probability_of_precipitation"]')
                        text_elem = forecast.find('text[@type="precis"]')
                        
                        min_temp = min_temp_elem.text if min_temp_elem is not None else 'N/A'
                        max_temp = max_temp_elem.text if max_temp_elem is not None else 'N/A'
                        precipitation = precip_elem.text if precip_elem is not None else 'N/A'
                        prob_precip = prob_precip_elem.text if prob_precip_elem is not None else 'N/A'
                        conditions = text_elem.text if text_elem is not None else 'N/A'
                        
                        # Use fallback for today's min/max if N/A
                        if index == "0":
                            if min_temp == 'N/A' and 'min' in fallback:
                                min_temp = fallback['min']
                            if max_temp == 'N/A' and 'max' in fallback:
                                max_temp = fallback['max']
                        elif index == "1":
                            tomorrow_min = min_temp
                            tomorrow_max = max_temp
                        
                        day = "Today" if index == "0" else "Tomorrow"
                        forecasts.append(f"{day}: Min {min_temp}°C, Max {max_temp}°C, Precipitation {precipitation}, Chance of Rain {prob_precip}, {conditions}")
                
                # Save tomorrow's temps as fallback for next day
                if tomorrow_min and tomorrow_min != 'N/A':
                    fallback['min'] = tomorrow_min
                if tomorrow_max and tomorrow_max != 'N/A':
                    fallback['max'] = tomorrow_max
                with open(fallback_file, 'w') as f:
                    json.dump(fallback, f)
                
                if forecasts:
                    return "; ".join(forecasts)
        return "Forecast data not found"
    except Exception as e:
        print(f"Error fetching BOM weather: {e}")
        return "Weather data unavailable"

def get_reddit_trends():
    """Get trending posts on Reddit via RSS"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; NewsEmailBot/1.0)'}
        response = requests.get('https://www.reddit.com/r/all/top/.rss?t=day&limit=50', headers=headers)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        trending_html = "<ul style='margin: 0; padding-left: 20px;'>"
        for entry in feed.entries[:5]:
            title = entry.title
            link = entry.link
            trending_html += f"<li><a href='{link}' style='color: #3498db;'>{title}</a></li>"
        trending_html += "</ul>"
        return trending_html
    except Exception as e:
        print(f"Error fetching Reddit RSS: {e}")
        return "Reddit trends unavailable"

def send_email(summary, config):
    """Send HTML summary via Gmail SMTP to multiple recipients"""
    html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                margin: 0;
                padding: 20px;
                background-color: #f4f4f4;
                color: #333;
                line-height: 1.6;
            }}
            .container {{
                max-width: 800px;
                margin: 0 auto;
                background-color: #ffffff;
                padding: 30px;
                border-radius: 8px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            h1 {{
                color: #2c3e50;
                font-size: 28px;
                margin-bottom: 30px;
                text-align: center;
                border-bottom: 3px solid #3498db;
                padding-bottom: 10px;
            }}
            h2 {{
                color: #34495e;
                font-size: 22px;
                margin-top: 40px;
                margin-bottom: 15px;
                border-left: 4px solid #3498db;
                padding-left: 15px;
            }}
            ul {{
                list-style-type: none;
                padding-left: 0;
            }}
            li {{
                margin-bottom: 15px;
                padding: 10px;
                background-color: #ecf0f1;
                border-radius: 5px;
                border-left: 4px solid #bdc3c7;
            }}
            li strong {{
                color: #2c3e50;
                font-weight: bold;
            }}
            a {{
                color: #3498db;
                text-decoration: none;
                font-weight: 500;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            .footer {{
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #ecf0f1;
                text-align: center;
                color: #7f8c8d;
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Daily News Summary</h1>
            {summary}
            <div class="footer">
                <p>{datetime.now().strftime('%Y-%m-%d')}</p>
            </div>
        </div>
    </body>
    </html>
    """
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(config['gmail_user'], config['gmail_app_password'])
        
        for recipient in config['recipient_emails']:
            msg = MIMEMultipart('alternative')
            msg['From'] = config['gmail_user']
            msg['To'] = recipient
            msg['Subject'] = f"Daily News Summary - {datetime.now().strftime('%Y-%m-%d')}"
            msg.attach(MIMEText(html, 'html'))
            text = msg.as_string()
            server.sendmail(config['gmail_user'], recipient, text)
        
        server.quit()
        print(f"Email sent successfully to {len(config['recipient_emails'])} recipients.")
    except Exception as e:
        print(f"Error sending email: {e}")

def main():
    config = load_config()
    
    # Fetch news by sections
    sections = {}
    
    # AI News - focus on AI with releases/announcements
    sections['AI News'] = fetch_newsapi_news(config['newsapi_key'], query='(AI OR "Artificial Intelligence") AND (release OR announcement OR product)', days_back=1)
    
    # Major International News - major sources
    sections['Major International News'] = fetch_newsapi_news(config['newsapi_key'], query='world news OR international OR politics', days_back=1, sources='bbc-news,nytimes,the-guardian,al-jazeera-english,associated-press,politico,reuters,the-washington-post,bloomberg,cnn')
    
    # Australian News - major Australian sources
    sections['Australian News'] = fetch_newsapi_news(config['newsapi_key'], query='Australia', days_back=1, sources='abc-news-au,sydney-morning-herald,the-australian, australian-financial-review,google-news-au,news-com-au')

    # Sports News - cricket, F1, athletics, soccer, running
    sections['Sports News'] = fetch_newsapi_news(config['newsapi_key'], query='cricket OR "F1" OR athletics OR soccer OR running', days_back=1)

    # Tech News - from specified sources
    sections['Tech News'] = fetch_newsapi_news(config['newsapi_key'], query='technology OR tech', days_back=1, sources='techcrunch,hacker-news,wired,recode,techradar,the-next-web')

    # Long-Form Articles - from specified sources
    sections['Long-Form Articles'] = fetch_newsapi_news(config['newsapi_key'], query='in-depth OR analysis OR long-form', days_back=1, sources='the-atlantic,new-yorker,new-york-magazine,national-geographic,new-scientist')

    # Trending on Social Media
    sections['Trending on Social Media'] = fetch_newsapi_news(config['newsapi_key'], query='trending OR viral', days_back=1, sources='buzzfeed,mtv-news,mashable,reddit-r-all,the-lad-bible')
    
    # Add RSS for more sources (major publications as fallback)
    rss_articles = fetch_rss_news([
        'http://feeds.bbci.co.uk/news/rss.xml',
        'http://feeds.reuters.com/Reuters/worldNews',
        'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
        'https://www.theguardian.com/world/rss',
        'https://www.economist.com/rss',
        'https://www.ft.com/rss/home/uk',
        # Australian sources
        'http://www.abc.net.au/news/feed/51120/rss.xml',
        'https://www.smh.com.au/rss/feed.xml',
    ])
    # Distribute RSS articles to sections (simple categorization), allow specific sports
    for article in rss_articles:
        content = (article['title'] + ' ' + article['content']).lower()
        sports_keywords = ['cricket', 'f1', 'formula 1', 'athletics', 'soccer', 'running', 'marathon']
        if any(word in content for word in sports_keywords):
            sections['Sports News'].append(article)
        elif any(word in content for word in ['football', 'basketball', 'tennis', 'rugby', 'olympics', 'sports']) and not any(word in content for word in sports_keywords):
            continue  # Skip other sports
        elif 'ai' in content or 'artificial intelligence' in content or 'machine learning' in content:
            sections['AI News'].append(article)
        elif 'australia' in content or 'australian' in content or article['source'].lower() in ['abc news', 'sydney morning herald', 'the australian']:
            sections['Australian News'].append(article)
        elif 'trending' in content or 'viral' in content:
            sections['Trending on Social Media'].append(article)
        else:
            sections['Major International News'].append(article)
    
    # Check if any articles
    total_articles = sum(len(arts) for arts in sections.values())
    if total_articles == 0:
        print("No articles fetched.")
        return
    
    # Get additional data
    weather = get_weather(config)
    reddit_trends = get_reddit_trends()

    # Summarize news
    news_summary = summarize_news(sections, config['llama3'])

    # Combine into full summary
    full_summary = f"""
    <div style="background-color: #e8f4f8; padding: 20px; margin-bottom: 30px; border-radius: 8px;">
        <h2 style="color: #2c3e50; margin-top: 0;">Today's Overview</h2>
        <p><strong>Weather in Eleebana, NSW:</strong> {weather}</p>
        <p><strong>Trending on Reddit (Top Today):</strong> {reddit_trends}</p>
    </div>
    {news_summary}
    <div style="background-color: #e8f4f8; padding: 20px; margin-bottom: 30px; border-radius: 8px;">
        <p><strong>Trending on Reddit (Top Today):</strong> {reddit_trends}</p>
    </div>
    """

    # Send email
    send_email(full_summary, config)

if __name__ == "__main__":
    main()
