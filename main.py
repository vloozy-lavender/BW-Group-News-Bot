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
#
# method:
#   "cloudscraper" — static HTML, no browser needed
#   "playwright"    — JS-rendered listing/content, needs a real browser
#   "blocked"       — confirmed Cloudflare bot-challenge as of last
#                     inspection (checked 3x: cloudscraper, Playwright
#                     w/ networkidle, Playwright w/ domcontentloaded — all
#                     hit the actual challenge page, not the site). This
#                     is NOT a selector problem. Skipped cleanly at
#                     runtime with a clear log line instead of quietly
#                     returning 0 and looking broken.
COMPANY_SITES = {
    "BW Group": {
        "url": "https://bw-group.com/newsroom",
        "method": "cloudscraper",
        "list_selector": "section.o-entity__body a",
        "date_selector": "time.o-entity__updated",
    },
    "BW Offshore": {
        # NOTE: /news-media doesn't carry the actual release list; this
        # (found via the site's own nav) does.
        "url": "https://www.bwoffshore.com/all-press-releases",
        "method": "playwright",  # Webflow CMS list, JS-rendered
        "list_selector": "div.w-dyn-item a",
        "date_selector": "div.text-block-433-copy",  # text only, e.g. "June 15, 2026"
    },
    "BW LPG": {
        # NOTE: /press-releases 404s. Real listing is under /media/.
        "url": "https://www.bwlpg.com/media/",
        "method": "playwright",  # WordPress but list renders client-side
        "list_selector": "div.article-container a",
        "date_selector": "time.updated",  # has datetime="" attr
    },
    "BW Energy": {
        "url": "https://www.bwenergy.no/news-and-media?category=press-releases",
        "method": "cloudscraper",
        "list_selector": "div.releases-body a",
        "date_selector": "time.updated",  # has datetime="" attr
    },
    "Hafnia": {
        "url": "https://investor.hafnia.com/news-events/press-releases",
        "method": "blocked",
        "list_selector": None,
        "date_selector": None,
    },
    "Navigator Gas": {
        "url": "https://investors.navigatorgas.com/news-events/press-releases",
        "method": "blocked",
        "list_selector": None,
        "date_selector": None,
    },
    "Cadeler": {
        "url": "https://ir.cadeler.com/press-releases",
        "method": "playwright",  # cloudscraper 404s on this one, needs JS
        "list_selector": "div.media-heading a",
        "date_selector": "time.date",  # has datetime="" attr
    },
    "BW Epic Kosan": {
        "url": "https://bwek.com/news",
        "method": "blocked",
        "list_selector": None,
        "date_selector": None,
    },
    "BW Ideol": {
        # NOTE: /category/financial-press-releases 404s. Real listing here.
        "url": "https://bw-ideol.com/en/latest-news",
        "method": "playwright",  # cloudscraper only sees nav, needs JS
        "list_selector": "div.ctnr a",
        "date_selector": "time",  # has datetime="" attr
    },
    "BW ESS": {
        "url": "https://bw-ess.com/news",
        "method": "cloudscraper",
        "list_selector": "h2 a",
        "date_selector": "div.news-date",  # text only, e.g. "01.07.2026" (DD.MM.YYYY)
    },
    "Corvus Energy": {
        "url": "https://corvusenergy.com/news",
        "method": "cloudscraper",  # listing works fine with cloudscraper
        "list_selector": "div.ssr-variant.hidden-vpufh4.hidden-h2smca a",
        "date_selector": None,  # article pages have NO date anywhere (checked
        # text, datetime attrs, and JSON-LD) — date is pulled from the
        # listing link's own text instead, see extract_listing_date_fallback()
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

# Support multiple recipients: EMAIL_TO secret can be "a@x.com,b@y.com"
_raw_email_to = os.getenv("EMAIL_TO", "vernonlee37@gmail.com")
EMAIL_TO = [email.strip() for email in _raw_email_to.split(",") if email.strip()]

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


def parse_date_dayfirst(raw):
    """Wraps date_parser.parse with dayfirst=True.

    Several sites (BW ESS: '01.07.2026', BW Ideol: '24/03/2026') use the
    European day-first numeric convention. dateutil does NOT assume this
    by default and will silently read '01.07.2026' as Jan 7 instead of
    Jul 1 without the flag — this was a real bug caught in testing.
    Month-name and ISO formats are unambiguous either way, so this is
    safe to apply everywhere.
    """
    if not raw:
        return None
    try:
        return date_parser.parse(raw, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def extract_listing_date_fallback(anchor_text):
    """Corvus Energy's article pages have no date anywhere (checked text,
    datetime attrs, and JSON-LD) — but the listing page's own link text
    embeds it, e.g. 'April 30, 2026Press ReleaseCorvus Energy Achieves...'.
    Used as a last-resort fallback when normal date extraction fails."""
    if not anchor_text:
        return None
    match = re.match(r"^([A-Z][a-z]+ \d{1,2},\s*\d{4})", anchor_text.strip())
    if match:
        return parse_date_dayfirst(match.group(1))
    return None

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
            
            # Capture (href, anchor_text) pairs, not just href — anchor
            # text is needed as a date fallback for sites like Corvus
            # Energy where the article page itself has no date at all.
            if list_selector:
                link_pairs = [(a['href'], a.get_text(strip=True)) for a in soup.select(list_selector) if a.get('href')]
            else:
                link_pairs = []
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg', 'tradingview', 'widget']):
                        continue
                    full_url = urljoin(news_url, href)
                    if len(href.split('/')) > 2:
                        link_pairs.append((full_url, a.get_text(strip=True)))
            
            # Cap per-site fetches to keep runtime sane, but don't stop
            # after the first match — a `break` here previously meant
            # each site could contribute at most 1 article per run,
            # regardless of how many were actually published that week.
            found_this_site = 0
            MAX_PER_SITE = 20
            for href, anchor_text in link_pairs:
                if found_this_site >= MAX_PER_SITE:
                    break
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
                                pub_date = parse_date_dayfirst(dt)
                    
                    if not pub_date:
                        meta_date = article_soup.find('meta', property='article:published_time')
                        if meta_date and meta_date.get('content'):
                            pub_date = parse_date_dayfirst(meta_date['content'])
                    
                    if not pub_date:
                        time_tag = article_soup.find('time')
                        if time_tag and time_tag.get('datetime'):
                            pub_date = parse_date_dayfirst(time_tag['datetime'])

                    if not pub_date:
                        # Last resort: date embedded in the listing link's
                        # own text (Corvus Energy pattern).
                        pub_date = extract_listing_date_fallback(anchor_text)
                    
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
                        found_this_site += 1
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
                page.wait_for_timeout(4000)  # let client-side rendering settle
                html = page.content()
                soup = BeautifulSoup(html, 'html.parser')
                
                if list_selector:
                    link_pairs = [(a['href'], a.get_text(strip=True)) for a in soup.select(list_selector) if a.get('href')]
                else:
                    link_pairs = []
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg']):
                            continue
                        full_url = urljoin(news_url, href)
                        if len(href.split('/')) > 2:
                            link_pairs.append((full_url, a.get_text(strip=True)))
                
                found_this_site = 0
                MAX_PER_SITE = 20
                for href, anchor_text in link_pairs:
                    if found_this_site >= MAX_PER_SITE:
                        break
                    full_url = href if href.startswith('http') else urljoin(news_url, href)
                    try:
                        page.goto(full_url, wait_until='domcontentloaded', timeout=30000)
                        page.wait_for_timeout(2000)
                        article_html = page.content()
                        article_soup = BeautifulSoup(article_html, 'html.parser')
                        
                        pub_date = None
                        if date_selector:
                            date_el = article_soup.select_one(date_selector)
                            if date_el:
                                dt = date_el.get('datetime') or date_el.get_text(strip=True)
                                if dt:
                                    pub_date = parse_date_dayfirst(dt)
                        
                        if not pub_date:
                            meta_date = article_soup.find('meta', property='article:published_time')
                            if meta_date and meta_date.get('content'):
                                pub_date = parse_date_dayfirst(meta_date['content'])
                        
                        if not pub_date:
                            time_tag = article_soup.find('time')
                            if time_tag and time_tag.get('datetime'):
                                pub_date = parse_date_dayfirst(time_tag['datetime'])

                        if not pub_date:
                            pub_date = extract_listing_date_fallback(anchor_text)
                        
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
                            found_this_site += 1
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
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

# =====================================================================
# SECTION 9: MAIN EXECUTION
# =====================================================================

def main():
    logging.info("Starting BW Group Weekly News Bot (Hybrid Mode)...")
    
    blocked = {k: v for k, v in COMPANY_SITES.items() if v['method'] == 'blocked'}
    if blocked:
        logging.warning(
            f"Skipping {len(blocked)} site(s) blocked by Cloudflare bot-protection "
            f"(confirmed, not a selector issue): {', '.join(blocked)}"
        )

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
    email_sent = send_email(html_content, start_date, end_date)

    if not email_sent:
        # Don't mark these items as archived if the email never went out —
        # otherwise they're silently skipped forever and no one notices.
        logging.error("Email failed to send. Archive was NOT updated, so these items will be retried next run.")
        raise SystemExit(1)  # make the GitHub Actions run show as failed

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
