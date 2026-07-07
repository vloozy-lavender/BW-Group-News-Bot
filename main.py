import os
import json
import logging
import time
import requests
import concurrent.futures
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from urllib.parse import urljoin
from groq import Groq
import resend
from collections import defaultdict
import urllib3
import cloudscraper
import feedparser
from playwright.sync_api import sync_playwright

# Disable SSL warnings for problematic corporate sites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# SECTION 1: CONFIGURATION & LIMITS
# =====================================================================

# RSS Feeds
RSS_FEEDS = {}

# Unified Company Sites Configuration
COMPANY_SITES = {
    "BW Group": {
        "url": "https://bw-group.com/newsroom",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Offshore": {
        "url": "https://www.bwoffshore.com/news-media",  # FIXED URL
        "method": "cloudscraper",
        "list_selector": "a.news-item",  # Targets article links
        "date_selector": None,  # No time tags found, falls back to generic
    },
    "BW LPG": {
        "url": "https://www.bwlpg.com/press-releases",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Energy": {
        "url": "https://www.bwenergy.no/news-and-media?category=press-releases",
        "method": "cloudscraper",
        "list_selector": None,  # Generic scanning works
        "date_selector": "time",  # Has <time> tags with datetime
    },
    "Hafnia": {
    "url": "https://investor.hafnia.com/ir-news/default.aspx",
    "method": "playwright",
    "list_selector": "a[href*='/ir-news/']",  # Targets links to news articles
    "date_selector": None,
    },
    "Navigator Gas": {
        "url": "https://investors.navigatorgas.com/news-events/press-releases",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "Cadeler": {
        "url": "https://ir.cadeler.com/press-releases",  # FIXED URL
        "method": "cloudscraper",
        "list_selector": "a[href*='/press-releases/detail/']",  # Targets article links
        "date_selector": "time.date",  # Found <time class="date"> on articles
    },
    "BW Epic Kosan": {
        "url": "https://bwek.com/news",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Ideol": {
        "url": "https://bw-ideol.com/category/financial-press-releases",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW ESS": {
        "url": "https://bw-ess.com/news",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "Corvus Energy": {
        "url": "https://corvusenergy.com/news",
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW LNG": {
        "url": None,  # TODO: find URL
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Dry Cargo": {
        "url": None,  # TODO: find URL
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Water": {
        "url": None,  # TODO: find URL
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Digital": {
        "url": None,  # TODO: find URL
        "method": "cloudscraper",
        "list_selector": None,
        "date_selector": None,
    },
}

# API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "news@yourdomain.com")
EMAIL_TO = os.getenv("EMAIL_TO", "vernonlee37@gmail.com")

ARCHIVE_FILE = "archive.json"

# Groq Rate Limit Controls
GROQ_MAX_CONCURRENT = 5
GROQ_BATCH_SIZE = 10

# =====================================================================
# SECTION 2: DATE LOGIC (Weekly: Last 7 days)
# =====================================================================

def get_date_range():
    today = datetime.now().date()
    start_date = today - timedelta(days=7)
    end_date = today
    return start_date, end_date

# =====================================================================
# SECTION 3: ARCHIVE MANAGEMENT (Deduplication)
# =====================================================================

def load_archive():
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, 'r') as f:
            return json.load(f)
    return []

def save_archive(archive):
    with open(ARCHIVE_FILE, 'w') as f:
        json.dump(archive, f, indent=2)

def is_new_item(url, archive):
    return not any(item['url'] == url for item in archive)

# =====================================================================
# SECTION 4: TIER 1 - RSS FEED SCRAPING (Fastest)
# =====================================================================

def scrape_rss_feeds():
    """Scrape news from RSS feeds. This is instant and 100% reliable."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping RSS feeds from {start_date} to {end_date}")
    collected_news = []
    
    for company_name, rss_url in RSS_FEEDS.items():
        if not rss_url:
            continue
            
        logging.info(f"Reading RSS feed for {company_name}...")
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries:
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6]).date()
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6]).date()
                
                if pub_date and start_date <= pub_date <= end_date:
                    title = entry.get('title', 'No Title')
                    link = entry.get('link', '')
                    summary = entry.get('summary', '')[:500]
                    
                    collected_news.append({
                        'url': link,
                        'title': title,
                        'date': pub_date,
                        'text': summary,
                        'company': company_name,
                        'source': 'rss'
                    })
                    logging.info(f"  Found via RSS: {title}")
        except Exception as e:
            logging.error(f"Failed to read RSS feed for {company_name}: {e}")
    
    logging.info(f"Found {len(collected_news)} articles via RSS")
    return collected_news

# =====================================================================
# SECTION 5: TIER 2 - CLOUDSCRAPER (Bypasses basic Cloudflare)
# =====================================================================

def scrape_with_cloudscraper():
    """Use cloudscraper to bypass basic anti-bot security."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping with cloudscraper from {start_date} to {end_date}")
    collected_news = []
    
    scraper = cloudscraper.create_scraper()
    
    for company_name, config in COMPANY_SITES.items():
        if config['method'] != 'cloudscraper' or not config['url']:
            continue
            
        news_url = config['url']
        list_selector = config['list_selector']
        date_selector = config['date_selector']
        
        logging.info(f"Scanning {company_name} with cloudscraper...")
        try:
            resp = scraper.get(news_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            if list_selector:
                links = [a['href'] for a in soup.select(list_selector) if a.get('href')]
            else:
                links = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg', 'tradingview', 'widget']):
                        continue
                    full_url = urljoin(news_url, href)
                    if len(href.split('/')) > 2:
                        links.append(full_url)
            
            for href in links:
                full_url = href if href.startswith('http') else urljoin(news_url, href)
                try:
                    article_resp = scraper.get(full_url, timeout=15)
                    article_soup = BeautifulSoup(article_resp.text, 'html.parser')
                    
                    pub_date = None
                    if date_selector:
                        date_el = article_soup.select_one(date_selector)
                        if date_el:
                            dt = date_el.get('datetime') or date_el.get_text(strip=True)
                            if dt:
                                try: pub_date = date_parser.parse(dt).date()
                                except: pass
                    
                    if not pub_date:
                        meta_date = article_soup.find('meta', property='article:published_time')
                        if meta_date and meta_date.get('content'):
                            try: pub_date = date_parser.parse(meta_date['content']).date()
                            except: pass
                    
                    if not pub_date:
                        time_tag = article_soup.find('time')
                        if time_tag and time_tag.get('datetime'):
                            try: pub_date = date_parser.parse(time_tag['datetime']).date()
                            except: pass
                    
                    if pub_date and start_date <= pub_date <= end_date:
                        title = article_soup.title.get_text(strip=True) if article_soup.title else "No Title"
                        for element in article_soup(["script", "style", "nav", "header", "footer"]):
                            element.extract()
                        paragraphs = article_soup.find_all('p')
                        text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
                        
                        collected_news.append({
                            'url': full_url,
                            'title': title,
                            'date': pub_date,
                            'text': text[:500],
                            'company': company_name,
                            'source': 'cloudscraper'
                        })
                        logging.info(f"  Found via cloudscraper: {title}")
                        break
                except:
                    continue
        except Exception as e:
            logging.error(f"Cloudscraper failed for {company_name}: {e}")
    
    logging.info(f"Found {len(collected_news)} articles via cloudscraper")
    return collected_news

# =====================================================================
# SECTION 6: TIER 3 - PLAYWRIGHT (Heavy JavaScript sites)
# =====================================================================

def scrape_with_playwright():
    """Use Playwright for sites with heavy JavaScript."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping with Playwright from {start_date} to {end_date}")
    collected_news = []
    
    playwright_sites = {k: v for k, v in COMPANY_SITES.items() if v['method'] == 'playwright' and v['url']}
    
    if not playwright_sites:
        logging.info("No Playwright sites configured")
        return collected_news
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for company_name, config in playwright_sites.items():
            news_url = config['url']
            list_selector = config['list_selector']
            date_selector = config['date_selector']
            
            logging.info(f"Scanning {company_name} with Playwright...")
            try:
                page.goto(news_url, wait_until='domcontentloaded', timeout=60000)
                html = page.content()
                soup = BeautifulSoup(html, 'html.parser')
                
                if list_selector:
                    links = [a['href'] for a in soup.select(list_selector) if a.get('href')]
                else:
                    links = []
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg']):
                            continue
                        full_url = urljoin(news_url, href)
                        if len(href.split('/')) > 2:
                            links.append(full_url)
                
                for href in links:
                    full_url = href if href.startswith('http') else urljoin(news_url, href)
                    try:
                        page.goto(full_url, wait_until='networkidle', timeout=30000)
                        article_html = page.content()
                        article_soup = BeautifulSoup(article_html, 'html.parser')
                        
                        pub_date = None
                        if date_selector:
                            date_el = article_soup.select_one(date_selector)
                            if date_el:
                                dt = date_el.get('datetime') or date_el.get_text(strip=True)
                                if dt:
                                    try: pub_date = date_parser.parse(dt).date()
                                    except: pass
                        
                        if not pub_date:
                            meta_date = article_soup.find('meta', property='article:published_time')
                            if meta_date and meta_date.get('content'):
                                try: pub_date = date_parser.parse(meta_date['content']).date()
                                except: pass
                        
                        if not pub_date:
                            time_tag = article_soup.find('time')
                            if time_tag and time_tag.get('datetime'):
                                try: pub_date = date_parser.parse(time_tag['datetime']).date()
                                except: pass
                        
                        if pub_date and start_date <= pub_date <= end_date:
                            title = article_soup.title.get_text(strip=True) if article_soup.title else "No Title"
                            for element in article_soup(["script", "style", "nav", "header", "footer"]):
                                element.extract()
                            paragraphs = article_soup.find_all('p')
                            text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
                            
                            collected_news.append({
                                'url': full_url,
                                'title': title,
                                'date': pub_date,
                                'text': text[:500],
                                'company': company_name,
                                'source': 'playwright'
                            })
                            logging.info(f"  Found via Playwright: {title}")
                            break
                    except:
                        continue
            except Exception as e:
                logging.error(f"Playwright failed for {company_name}: {e}")
        
        browser.close()
    
    logging.info(f"Found {len(collected_news)} articles via Playwright")
    return collected_news

# =====================================================================
# SECTION 7: AI PROCESSING (CONCURRENT BATCHING WITH RATE LIMITS)
# =====================================================================

def process_single_item(item):
    prompt = f"""You are a corporate news analyst. Analyze this single update from {item['company']}.

Text: {item['text']}
Title: {item['title']}

Return a JSON object with exactly these fields:
- "headline": A short, punchy headline (max 10 words).
- "summary": Exactly 1-2 sentences summarizing the key point.
- "category": MUST be exactly one of these: "New Development", "Progress/Update", "People Mentioned", "General News".

Return ONLY the JSON object. No markdown."""

    client = Groq(api_key=GROQ_API_KEY)
    
    for attempt in range(3):
        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
                response_format={"type": "json_object"}
            )
            parsed = json.loads(completion.choices[0].message.content)
            parsed['url'] = item['url']
            parsed['company'] = item['company']
            parsed['date'] = str(item['date'])
            return parsed
        except Exception as e:
            err_str = str(e).lower()
            if '429' in err_str or 'rate_limit' in err_str:
                logging.warning(f"Groq rate limit hit for {item['url']}. Retrying in 5s... (Attempt {attempt + 1}/3)")
                time.sleep(5)
            else:
                logging.error(f"Groq failed for {item['url']}: {e}")
                break
                
    return {
        'headline': item['title'],
        'summary': 'Summary unavailable.',
        'category': 'General News',
        'url': item['url'],
        'company': item['company'],
        'date': str(item['date'])
    }

def process_with_groq_concurrent(all_items):
    if not all_items:
        return []
    
    logging.info(f"Processing {len(all_items)} items with Groq concurrently...")
    processed_items = []
    
    total_chunks = (len(all_items) + GROQ_BATCH_SIZE - 1) // GROQ_BATCH_SIZE
    
    for i in range(0, len(all_items), GROQ_BATCH_SIZE):
        chunk = all_items[i:i+GROQ_BATCH_SIZE]
        chunk_num = i // GROQ_BATCH_SIZE + 1
        logging.info(f"Processing chunk {chunk_num}/{total_chunks} ({len(chunk)} items)...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=GROQ_MAX_CONCURRENT) as executor:
            futures = {executor.submit(process_single_item, item): item for item in chunk}
            for future in concurrent.futures.as_completed(futures):
                processed_items.append(future.result())
                
        if i + GROQ_BATCH_SIZE < len(all_items):
            time.sleep(2)
            
    return processed_items

# =====================================================================
# SECTION 8: HTML TABLE EMAIL GENERATION & DELIVERY
# =====================================================================

def generate_html_email(processed_items, start_date, end_date):
    grouped = defaultdict(list)
    for item in processed_items:
        cat = item.get('category', 'General News')
        grouped[cat].append(item)
        
    category_order = ["New Development", "Progress/Update", "People Mentioned", "General News"]
    
    table_style = 'border="1" cellpadding="10" cellspacing="0" style="border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size: 14px; color: #333; border-color: #ddd;"'
    th_style = 'style="background-color: #f4f4f4; text-align: left; padding: 10px; border: 1px solid #ddd;"'
    td_style = 'style="padding: 10px; border: 1px solid #ddd; vertical-align: top;"'

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.5; color: #333; max-width: 900px; margin: 0 auto;">
        <h1 style="color: #003366; font-size: 24px; margin-bottom: 5px;">BW Group Weekly Intelligence Digest</h1>
        <p style="color: #666; font-size: 14px; margin-top: 0;">
            <strong>Week of:</strong> {start_date.strftime('%b %d')} - {end_date.strftime('%b %d, %Y')}<br>
            <strong>Total Updates:</strong> {len(processed_items)} items tracked across all subsidiaries.
        </p>
        <hr style="border: 0; border-top: 2px solid #003366; margin: 20px 0;">
    """

    for category in category_order:
        items = grouped.get(category, [])
        if not items:
            continue
            
        items.sort(key=lambda x: x.get('date', '1970-01-01'), reverse=True)
        
        html += f"<h2 style='color: #0066cc; font-size: 18px; border-bottom: 2px solid #0066cc; padding-bottom: 5px; margin-top: 30px;'>{category} ({len(items)})</h2>"
        html += f"<table {table_style}>"
        html += f"<tr><th {th_style} style='width: 10%;'>Date</th><th {th_style} style='width: 15%;'>Company</th><th {th_style} style='width: 55%;'>Headline & Summary</th><th {th_style} style='width: 20%;'>Link</th></tr>"
        
        for item in items:
            raw_date = item.get('date', '')
            try:
                date_obj = datetime.strptime(raw_date, '%Y-%m-%d')
                formatted_date = date_obj.strftime('%b %d')
            except:
                formatted_date = raw_date
                
            headline = item.get('headline', 'No Headline')
            summary = item.get('summary', 'No summary.')
            url = item.get('url', '#')
            company = item.get('company', 'Unknown')
            
            html += f"""
            <tr>
                <td {td_style} style="width: 10%; white-space: nowrap; color: #666; font-size: 13px;">{formatted_date}</td>
                <td {td_style} style="font-weight: bold; width: 15%; background-color: #fafafa;">{company}</td>
                <td {td_style} style="width: 55%;">
                    <strong style="font-size: 15px;">{headline}</strong><br>
                    <span style="color: #555; font-size: 13px;">{summary}</span>
                </td>
                <td {td_style} style="width: 20%; text-align: center;">
                    <a href="{url}" style="color: #0066cc; text-decoration: none; font-weight: bold;">Read Original →</a>
                </td>
            </tr>
            """
        html += "</table>"

    html += """
    <hr style="border: 0; border-top: 1px solid #ddd; margin: 30px 0;">
    <p style="color: #999; font-size: 12px; text-align: center;">
        Generated automatically by the BW Group News Bot.
    </p>
    </body>
    </html>
    """
    return html

def send_email(html_content, start_date, end_date):
    resend.api_key = RESEND_API_KEY
    subject = f"BW Group Weekly Digest: {len(html_content.split('<tr>'))-1} Updates ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"
    
    try:
        params = {
            "from": EMAIL_FROM,
            "to": EMAIL_TO,
            "subject": subject,
            "html": html_content,
        }
        email = resend.Emails.send(params)
        logging.info(f"Email sent successfully: {email}")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

# =====================================================================
# SECTION 9: MAIN EXECUTION
# =====================================================================

def main():
    logging.info("Starting BW Group Weekly News Bot (Hybrid Mode)...")
    
    archive = load_archive()
    logging.info(f"Archive contains {len(archive)} previously sent items")
    
    rss_news = scrape_rss_feeds()
    cloudscraper_news = scrape_with_cloudscraper()
    playwright_news = scrape_with_playwright()
    
    all_items = rss_news + cloudscraper_news + playwright_news
    
    new_items = [item for item in all_items if is_new_item(item['url'], archive)]
    logging.info(f"After deduplication: {len(new_items)} new items")
    
    if not new_items:
        logging.info("No new items to report")
        return
    
    processed_items = process_with_groq_concurrent(new_items)
    
    start_date, end_date = get_date_range()
    html_content = generate_html_email(processed_items, start_date, end_date)
    send_email(html_content, start_date, end_date)
    
    for item in new_items:
        try:
            item['date'] = item['date'].strftime('%Y-%m-%d')
        except Exception as e:
            logging.warning(f"Could not convert date for {item.get('url')}: {e}")
            
    archive.extend(new_items)
    save_archive(archive)
    logging.info(f"Archive updated. Total items: {len(archive)}")
    
    logging.info("Bot finished successfully")

if __name__ == "__main__":
    main()
