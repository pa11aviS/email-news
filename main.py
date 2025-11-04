#!/usr/bin/env python3
"""
Daily News Summarizer and Emailer
Fetches news from RSS feeds and NewsAPI, summarizes with Ollama, and emails to Gmail.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
import requests
import ollama
import xml.etree.ElementTree as ET
from urllib.request import urlopen
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

def fetch_newsapi_news(
    api_key,
    query=None,
    days_back=1,
    sources=None,      # comma-separated NewsAPI source IDs
    language="en",
    sort_by="publishedAt",
    page_size=15,
):
    base = "https://newsapi.org/v2/everything"
    now = datetime.now(timezone.utc)
    to_dt = now - timedelta(hours=24)
    from_date = to_dt - timedelta(days=days_back)
    params = {
        "q": query or "",
        "from": from_date,
        "to":   to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sortBy": sort_by,
        "language": language,
        "pageSize": min(page_size, 100),
    }
    if sources:
        params["sources"] = sources  # <-- keep using IDs, not domains

    try:
        r = requests.get(base, params=params, headers={"X-Api-Key": api_key}, timeout=20)
        if r.status_code == 429:
            print("NewsAPI rate limit hit (429). Try reducing calls or upgrading your plan.")
            return []
        if r.status_code == 400:
            print(f"NewsAPI 400: {r.text}")  # often indicates a bad source ID
            return []
        r.raise_for_status()
        data = r.json()
        out = []
        for a in data.get("articles", []):
            src = a.get("source") or {}
            out.append({
                "title": a.get("title") or "",
                "content": a.get("content") or (a.get("description") or ""),
                "url": a.get("url"),
                "source": src.get("name") or src.get("id") or "Unknown",
                "published": a.get("publishedAt"),
            })
        return out
    except Exception as e:
        print(f"Error fetching from NewsAPI: {e}")
        return []

def summarize_news(sections, model_name, per_section_limit=7, per_section_pool=12):
    """
    Curate and format news per section WITHOUT cross-section mixing.
    Keeps the original "guides" for how to select items.
    Prompts Ollama once per section with only that section's candidates.
    """

    SECTION_ORDER = [
        'AI News',
        'Major International News',
        'Australian News',
        'Sports News',
        'Tech News',
        'Long-Form Articles',
        'Trending on Social Media',
    ]

    # Your original guides preserved verbatim
    SECTION_GUIDES = {
        'AI News': "AI NEWS: Articles about artificial intelligence, machine learning, AI releases.",
        'Major International News': "MAJOR INTERNATIONAL NEWS: Global news, politics, world events.",
        'Australian News': "AUSTRALIAN NEWS: News from or about Australia.",
        'Sports News': "SPORTS NEWS: Cricket, F1, athletics, soccer, running only.",
        'Tech News': "TECH NEWS: Technology, gadgets, software.",
        'Long-Form Articles': "LONG-FORM ARTICLES: In-depth analysis, features.",
        'Trending on Social Media': "TRENDING ON SOCIAL MEDIA: Viral topics, trends.",
    }

    # Build a single "guides" block that we show in every section prompt
    all_guides_text = "\n".join([
        "- " + SECTION_GUIDES[name] for name in SECTION_ORDER
    ])

    def _to_dt(x):
        # Best-effort sort by published desc
        val = x.get('published')
        if not val:
            return datetime.min
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return datetime.min

    def _format_article_for_prompt(idx, art):
        src = (art.get('source') or 'Unknown')
        title = (art.get('title') or '').strip()
        content = (art.get('content') or '').strip()
        return f"{idx}. Title: {title}\n   Content: {content[:220]}\n   Source: {src}"

    def _pick_indices_with_ollama(section_name, numbered_block):
        """
        Ask for comma-separated numbers only, but include the ORIGINAL guides block
        so the model has the same criteria text you used before.
        Crucially: we tell it it can ONLY choose from THIS section's list.
        """
        prompt = f"""You are a journalist curating an email newsletter for a highly-educated professional audience.

Here are the section selection guides (for reference, unchanged from the original code):
{all_guides_text}

You are curating ONLY this section now: {section_name}

Below is a numbered list of articles that ALREADY belong to {section_name}. 
Select the best {min(per_section_limit, 7)} by returning ONLY a comma-separated list of numbers (e.g. "1,3,5").
Do NOT add any extra text. Do NOT reference other sections. Only pick from the numbers shown.

Articles:
{numbered_block}
"""
        try:
            resp = ollama.chat(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = resp["message"]["content"].strip()
            # Parse comma-separated integers only
            out = []
            for tok in raw.replace("\n", ",").split(","):
                tok = tok.strip()
                if tok.isdigit():
                    out.append(int(tok))
            return out
        except Exception:
            return []

    html_parts = []

    for section_name in SECTION_ORDER:
        arts = sections.get(section_name, []) or []
        if not arts:
            continue

        # Sort newest first, then take a manageable pool
        arts_sorted = sorted(arts, key=_to_dt, reverse=True)
        pool = arts_sorted[:per_section_pool]

        # Build the numbered list just for THIS section
        numbered_lines = []
        local_map = {}
        for i, art in enumerate(pool, start=1):
            numbered_lines.append(_format_article_for_prompt(i, art))
            local_map[i] = art
        numbered_block = "\n\n".join(numbered_lines)

        # Ask Ollama to pick indices — but ONLY from this section's pool
        picks = _pick_indices_with_ollama(section_name, numbered_block)

        # Keep valid, unique indices; fallback to top-N if model returns junk/empty
        seen = set()
        chosen = []
        for i in picks:
            if i in local_map and i not in seen:
                chosen.append(i)
                seen.add(i)

        if not chosen:
            chosen = list(range(1, min(len(pool), per_section_limit) + 1))

        # Render HTML for this section
        section_html = [f"<h2>{section_name.upper()}</h2>", "<ul>"]
        for i in chosen[:per_section_limit]:
            art = local_map[i]
            title = (art.get('title') or '').strip()
            summary = (art.get('content') or '').strip()[:300]
            link = art.get('url') or '#'
            source = (art.get('source') or 'Unknown')
            section_html.append(
                f"<li><strong>{title}</strong>: {summary} <a href='{link}'>[{source}]</a></li>"
            )
        section_html.append("</ul>")
        html_parts.append("\n".join(section_html))

    return "\n\n".join(html_parts)

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
        
def validate_source_ids(api_key, csv_ids):
    try:
        r = requests.get(
            "https://newsapi.org/v2/top-headlines/sources",
            headers={"X-Api-Key": api_key},
            timeout=20,
        )
        r.raise_for_status()
        valid = {s["id"] for s in r.json().get("sources", []) if s.get("id")}
        req = [s.strip() for s in csv_ids.split(",") if s.strip()]
        bad = [s for s in req if s not in valid]
        good = [s for s in req if s in valid]
        if bad:
            print(f"[WARN] Invalid NewsAPI IDs removed: {', '.join(bad)}")
        return ",".join(good)
    except Exception as e:
        print(f"[WARN] Could not validate source IDs: {e}")
        return csv_ids

def main():
    config = load_config()
    
    # Fetch news by sections
    sections = {}

    major_ids = validate_source_ids(
    config['newsapi_key'],
    'bbc-news,nytimes,the-guardian,al-jazeera-english,associated-press,politico,reuters,the-washington-post,bloomberg,cnn,abc-news-au,sydney-morning-herald,the-australian,australian-financial-review,google-news-au,news-com-au,techcrunch,hacker-news,wired,recode,techradar,the-next-web,the-atlantic,new-yorker,new-york-magazine,national-geographic,new-scientist,buzzfeed,mtv-news,mashable,reddit-r-all,the-lad-bible'
    )
    
    sections['AI News'] = fetch_newsapi_news(
        config['newsapi_key'],
        query='("machine learning" OR "artificial intelligence") AND (release OR announcement OR product)',
        days_back=1
    )

    # Major International News (IDs only)
    sections['Major International News'] = fetch_newsapi_news(
        config['newsapi_key'],
        days_back=1,
        sources='bbc-news,nytimes,the-guardian,al-jazeera-english,associated-press,politico,reuters,the-washington-post,bloomberg,cnn'
    )

    # Australian News (IDs only — remove the stray space)
    sections['Australian News'] = fetch_newsapi_news(
        config['newsapi_key'],
        query='Australia',
        days_back=1,
        sources='abc-news-au,sydney-morning-herald,the-australian,australian-financial-review,google-news-au,news-com-au'
    )

    # Sports News
    sections['Sports News'] = fetch_newsapi_news(
        config['newsapi_key'],
        query='cricket OR "F1" OR athletics OR soccer OR running',
        days_back=1
    )

    # Tech News
    sections['Tech News'] = fetch_newsapi_news(
        config['newsapi_key'],
        query='technology OR tech',
        days_back=1,
        sources='techcrunch,hacker-news,wired,recode,techradar,the-next-web'
    )

    # Long-Form
    sections['Long-Form Articles'] = fetch_newsapi_news(
        config['newsapi_key'],
        days_back=1,
        sources='the-atlantic,new-yorker,new-york-magazine,national-geographic,new-scientist'
    )

    # Trending on Social Media
    sections['Trending on Social Media'] = fetch_newsapi_news(
        config['newsapi_key'],
        query='trending OR viral',
        days_back=1,
        sources='buzzfeed,mtv-news,mashable,reddit-r-all,the-lad-bible'
    )
    
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
        elif any(word in content for word in ['football', 'basketball', 'rugby', 'sports']) and not any(word in content for word in sports_keywords):
            continue  # Skip other sports
        elif 'artificial intelligence' in content or 'machine learning' in content:
            sections['AI News'].append(article)
        elif 'australia' in content or 'australian' in content or article['source'].lower() in ['abc news', 'sydney morning herald', 'the australian']:
            sections['Australian News'].append(article)
        elif 'trending' in content or 'viral' in content:
            sections['Trending on Social Media'].append(article)
        else:
            sections['Major International News'].append(article)

    SECTION_ORDER = [
    'AI News',
    'Major International News',
    'Australian News',
    'Sports News',
    'Tech News',
    'Long-Form Articles',
    'Trending on Social Media',
    ]
    for name in SECTION_ORDER:
        sections.setdefault(name, [])

    # Build a simple block listing missing sections
    missing = [name for name in SECTION_ORDER if len(sections[name]) == 0]
    missing_html = ""
    if missing:
        missing_html = (
            "<div style='background:#fff7e6;border:1px solid #ffe7ba;"
            "padding:16px;border-radius:8px;margin:20px 0;'>"
            "<h2 style='margin:0 0 8px 0;'>Sections with no items</h2>"
            "<ul style='margin:0;padding-left:18px;'>"
            + "".join(f"<li><strong>{name}</strong>: 0 articles found</li>" for name in missing)
            + "</ul></div>"
        )
    
    # Check if any articles
    total_articles = sum(len(arts) for arts in sections.values())
    if total_articles == 0:
        print("No articles fetched.")
        return
    
    # Get additional data
    weather = get_weather(config)
    reddit_trends = get_reddit_trends()

    # Summarize news
    news_summary = summarize_news(sections, "llama3")

    # Combine into full summary
    full_summary = f"""
    <div style="background-color: #e8f4f8; padding: 20px; margin-bottom: 30px; border-radius: 8px;">
        <h2 style="color: #2c3e50; margin-top: 0;">Today's Overview</h2>
        <p><strong>Weather in Eleebana, NSW:</strong> {weather}</p>
    </div>
    {news_summary}
    {missing_html}
    <div style="background-color: #e8f4f8; padding: 20px; margin-bottom: 30px; border-radius: 8px;">
        <p><strong>Trending on Reddit (Top Today):</strong> {reddit_trends}</p>
    </div>
    """

    # Send email
    send_email(full_summary, config)

if __name__ == "__main__":
    main()
