import os
import json
import logging
import requests
import concurrent.futures
import re
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from urllib.parse import urljoin
from groq import Groq
from apify_client import ApifyClient
import resend
from collections import defaultdict
import urllib3
import cloudscraper
import feedparser
from playwright.sync_api import sync_playwright

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# SECTION 1: CONFIGURATION & LIMITS
# =====================================================================

# RSS Feeds - NONE FOUND (all companies lack RSS feeds)
RSS_FEEDS = {}

# Sites to scrape with cloudscraper (bypasses basic Cloudflare)
# These are faster and work on 80% of corporate sites
CLOUDSCRAPER_SITES = {
    "BW Group": "https://bw-group.com/newsroom",
    "BW Offshore": "https://www.bwoffshore.com/investors/press-releases",
    "BW LPG": "https://www.bwlpg.com/press-releases",
    "BW Energy": "https://www.bwenergy.no/news-and-media?category=press-releases",
    "Hafnia": "https://investor.hafnia.com/news-events/press-releases",
    "Navigator Gas": "https://investors.navigatorgas.com/news-events/press-releases",
    "Cadeler": "https://ir.cadeler.com/news-events/press-releases",
    "BW Epic Kosan": "https://bwek.com/news",
    "BW Ideol": "https://bw-ideol.com/category/financial-press-releases",
    "BW ESS": "https://bw-ess.com/news",
    "Corvus Energy": "https://corvusenergy.com/news",
}

# Sites that need Playwright (heavy JavaScript - last resort)
# Leave this empty for now. If cloudscraper fails on any site above,
# we'll move it here after testing.
PLAYWRIGHT_SITES = {}

# LinkedIn company pages (via Apify - already working)
LINKEDIN_COMPANIES = [
    "https://www.linkedin.com/company/bw-group/",
    "https://www.linkedin.com/company/bw-offshore/",
    "https://www.linkedin.com/company/bw-lpg/",
    "https://www.linkedin.com/company/bw-lng/",
    "https://www.linkedin.com/company/hafnia/",
    "https://www.linkedin.com/company/bw-energy/",
    "https://www.linkedin.com/company/navigator-gas/",
    "https://www.linkedin.com/company/cadeler/",
    "https://www.linkedin.com/company/bw-dry-cargo/",
    "https://www.linkedin.com/company/bw-epic-kosan/",
    "https://www.linkedin.com/company/bw-water/",
    "https://www.linkedin.com/company/bw-ideol/",
    "https://www.linkedin.com/company/bw-digital/",
    "https://www.linkedin.com/company/bw-ess/",
    "https://www.linkedin.com/company/corvus-energy/",
]

# API Keys
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "news@yourdomain.com")
EMAIL_TO = os.getenv("EMAIL_TO", "vernonlee37@gmail.com")

ARCHIVE_FILE = "archive.json"
MAX_LINKEDIN_POSTS = 30

# =====================================================================
# SECTION 2: DATE LOGIC
# =====================================================================

def get_date_range():
    today = datetime.now().date()
    start_date = today - timedelta(days=7)
    end_date = today
    return start_date, end_date

# =====================================================================
# SECTION 3: ARCHIVE MANAGEMENT
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
                # Extract date
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
    
    for company_name, news_url in CLOUDSCRAPER_SITES.items():
        logging.info(f"Scanning {company_name} with cloudscraper...")
        try:
            resp = scraper.get(news_url, timeout=15)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Find article links
            for a in soup.find_all('a', href=True):
                href = a['href']
                if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg', 'tradingview', 'widget']):
                    continue
                
                full_url = urljoin(news_url, href)
                if len(href.split('/')) > 2:
                    # Visit the article page
                    try:
                        article_resp = scraper.get(full_url, timeout=15)
                        article_soup = BeautifulSoup(article_resp.text, 'html.parser')
                        
                        # Extract date
                        pub_date = None
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
                            break  # Only get first article per link to avoid duplicates
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
    
    if not PLAYWRIGHT_SITES:
        logging.info("No Playwright sites configured")
        return collected_news
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        for company_name, news_url in PLAYWRIGHT_SITES.items():
            logging.info(f"Scanning {company_name} with Playwright...")
            try:
                page.goto(news_url, wait_until='networkidle', timeout=30000)
                html = page.content()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Find article links and extract data (similar to cloudscraper)
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg']):
                        continue
                    
                    full_url = urljoin(news_url, href)
                    if len(href.split('/')) > 2:
                        try:
                            page.goto(full_url, wait_until='networkidle', timeout=30000)
                            article_html = page.content()
                            article_soup = BeautifulSoup(article_html, 'html.parser')
                            
                            pub_date = None
                            meta_date = article_soup.find('meta', property='article:published_time')
                            if meta_date and meta_date.get('content'):
                                try: pub_date = date_parser.parse(meta_date['content']).date()
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
# SECTION 7: LINKEDIN SCRAPING VIA APIFY
# =====================================================================

def scrape_linkedin():
    start_date, end_date = get_date_range()
    logging.info(f"Scraping LinkedIn from {start_date} to {end_date}")
    
    client = ApifyClient(APIFY_API_TOKEN)
    run_input = {
        "targetUrls": LINKEDIN_COMPANIES,
        "maxResults": MAX_LINKEDIN_POSTS,
        "includeQuotePosts": False,
        "includeReposts": False,
    }
    
    logging.info("Starting Apify LinkedIn scraper...")
    run = client.actor("harvestapi/linkedin-company-posts").call(run_input=run_input)
    
    collected_posts = []
    for item in client.dataset(run.default_dataset_id).iterate_items():
        try:
            post_date = date_parser.parse(item.get('date', '')).date()
        except:
            continue
        
        if start_date <= post_date <= end_date:
            company_url = item.get('companyUrl', '')
            company_name = company_url.split('/')[-2].replace('-', ' ').title() if company_url else "Unknown"
            
            collected_posts.append({
                'url': item.get('url', ''),
                'title': item.get('text', '')[:100] + "..." if len(item.get('text', '')) > 100 else item.get('text', ''),
                'date': post_date,
                'text': item.get('text', '')[:500],
                'company': company_name,
                'source': 'linkedin'
            })
            logging.info(f"  Found LinkedIn post: {item.get('text', '')[:50]}")
    
    logging.info(f"Found {len(collected_posts)} LinkedIn posts in date range")
    return collected_posts

# =====================================================================
# SECTION 8: AI PROCESSING (CONCURRENT BATCHING)
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
        logging.error(f"Groq failed for {item['url']}: {e}")
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
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_single_item, item): item for item in all_items}
        for future in concurrent.futures.as_completed(futures):
            processed_items.append(future.result())
            
    return processed_items

# =====================================================================
# SECTION 9: HTML TABLE EMAIL GENERATION & DELIVERY
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
# SECTION 10: MAIN EXECUTION
# =====================================================================

def main():
    logging.info("Starting BW Group Weekly News Bot (Hybrid Mode)...")
    
    archive = load_archive()
    logging.info(f"Archive contains {len(archive)} previously sent items")
    
    # TIER 1: RSS Feeds (Fastest)
    rss_news = scrape_rss_feeds()
    
    # TIER 2: Cloudscraper (Bypasses basic security)
    cloudscraper_news = scrape_with_cloudscraper()
    
    # TIER 3: Playwright (Heavy JS sites)
    playwright_news = scrape_with_playwright()
    
    # LinkedIn (via Apify)
    linkedin_posts = scrape_linkedin()
    
    # Combine all sources
    all_items = rss_news + cloudscraper_news + playwright_news + linkedin_posts
    
    # Deduplicate
    new_items = [item for item in all_items if is_new_item(item['url'], archive)]
    logging.info(f"After deduplication: {len(new_items)} new items")
    
    if not new_items:
        logging.info("No new items to report")
        return
    
    # Process with AI
    processed_items = process_with_groq_concurrent(new_items)
    
    # Generate and send email
    start_date, end_date = get_date_range()
    html_content = generate_html_email(processed_items, start_date, end_date)
    send_email(html_content, start_date, end_date)
    
    # Update archive
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
