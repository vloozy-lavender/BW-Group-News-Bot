import os
import json
import logging
import requests
import concurrent.futures
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from urllib.parse import urljoin
from groq import Groq
from apify_client import ApifyClient
import resend
from collections import defaultdict
import urllib3

# Disable SSL warnings for problematic corporate sites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# SECTION 1: CONFIGURATION & LIMITS
# =====================================================================

# Official Corporate & Investor Relations Pages
COMPANIES_TO_TRACK = {
    "BW Group (Press Releases)": "https://bw-group.com/newsroom?category=Press%20Release",
    "BW Group (All News)": "https://bw-group.com/newsroom",
    "BW Offshore (IR)": "https://www.bwoffshore.com/investors/press-releases",
    "BW Offshore (News)": "https://www.bwoffshore.com/news",
    "BW LNG (News)": "https://bw-group.com/newsroom?category=News&company=BW%20LNG",
    "BW LPG (Press Releases)": "https://www.bwlpg.com/press-releases",
    "BW LPG (Stock Announcements)": "https://www.bwlpg.com/investors/stock-exchange-announcements",
    "BW Epic Kosan (News)": "https://bwek.com/news",
    "BW Energy (Press Releases)": "https://www.bwenergy.no/news-and-media?category=press-releases",
    "BW Energy (All News)": "https://www.bwenergy.no/news-and-media",
    "Hafnia (Press Releases)": "https://investor.hafnia.com/news-events/press-releases",
    "Hafnia (Stock Announcements)": "https://investor.hafnia.com/news-events/stock-exchange-announcements",
    "Navigator Gas (Press Releases)": "https://investors.navigatorgas.com/news-events/press-releases",
    "Cadeler (Press Releases)": "https://ir.cadeler.com/news-events/press-releases",
    "BW Dry Cargo (News)": "https://bw-group.com/newsroom?category=News&company=BW%20Dry%20Cargo",
    "BW Water (News)": "https://bw-group.com/newsroom?category=News&company=BW%20Water",
    "BW Ideol (Financial)": "https://bw-ideol.com/category/financial-press-releases",
    "BW Digital (News)": "https://bw-group.com/newsroom?category=News&company=BW%20Digital",
    "BW ESS (News)": "https://bw-ess.com/news",
    "Corvus Energy (News)": "https://corvusenergy.com/news",
}

# Third-Party Industry & Financial Sources
THIRD_PARTY_SOURCES = {
    "TradeWinds (BW Group)": "https://www.tradewindsnews.com/search?q=BW+Group",
    "TradeWinds (Hafnia)": "https://www.tradewindsnews.com/search?q=Hafnia",
    "Splash247 (BW Group)": "https://splash247.com/search/BW+Group",
    "World Ports (BW Group)": "https://www.worldports.org/search?q=BW+Group",
    "Maritime Executive (BW Group)": "https://maritime-executive.com/search?q=BW+Group",
    "Bloomberg (BW Group)": "https://www.bloomberg.com/search?query=BW+Group",
    "Reuters (BW Group)": "https://www.reuters.com/search/news?blob=BW+Group",
    "Finansavisen (Hafnia)": "https://finansavisen.no/search?q=Hafnia",
    "Bunker Index (Hafnia)": "https://bunkerindex.com/news/?search=Hafnia",
    "GlobeNewswire (BW Offshore)": "https://www.globenewswire.com/search?q=BW+Offshore",
}

# LinkedIn company pages to track (All 15 subsidiaries)
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

# API Keys (Loaded from GitHub Secrets)
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "news@yourdomain.com")
EMAIL_TO = os.getenv("EMAIL_TO", "vernonlee37@gmail.com") # Keep your email for testing!

# Archive file path
ARCHIVE_FILE = "archive.json"

# SCRAPING LIMITS (To protect Apify free tier and LLM context)
MAX_LINKEDIN_POSTS = 30  # Max posts per company on LinkedIn
MAX_WEBSITE_ARTICLES = 15 # Max articles per source on websites

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
# SECTION 4: WEBSITE SCRAPING
# =====================================================================

def get_article_links(main_url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(main_url, timeout=15, headers=headers, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        links = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Ignore social links, emails, downloads, and embedded widgets
            if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg', '.png', 'tradingview', 'widget', 'iframe', 'youtube', 'javascript:', 'cookie']):
                continue
            full_url = urljoin(main_url, href)
            if len(href.split('/')) > 2:
                links.add(full_url)
        return list(links)
    except Exception as e:
        logging.error(f"Failed to get links from {main_url}: {e}")
        return []

def extract_article_data(article_url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(article_url, timeout=15, headers=headers, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        pub_date = None
        meta_date = soup.find('meta', property='article:published_time')
        if meta_date and meta_date.get('content'):
            try: pub_date = date_parser.parse(meta_date['content']).date()
            except: pass
        
        if not pub_date:
            time_tag = soup.find('time')
            if time_tag and time_tag.get('datetime'):
                try: pub_date = date_parser.parse(time_tag['datetime']).date()
                except: pass

        # Safer title extraction
        title = soup.title.get_text(strip=True) if soup.title else "No Title"
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].strip()
        
        for element in soup(["script", "style", "nav", "header", "footer"]):
            element.extract()
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
        
        return {
            'url': article_url,
            'title': title,
            'date': pub_date, # Kept as date object for comparison
            'text': text[:500],
            'source': 'website'
        }
    except Exception as e:
        logging.error(f"Failed to process {article_url}: {e}")
        return None

def scrape_websites():
    """Scrape all official company websites AND third-party industry sources."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping websites from {start_date} to {end_date}")
    collected_news = []
    
    # Combine both dictionaries so the bot scrapes everything
    all_sources = {**COMPANIES_TO_TRACK, **THIRD_PARTY_SOURCES}
    
    for source_name, news_url in all_sources.items():
        logging.info(f"Scanning {source_name}...")
        links = get_article_links(news_url)
        company_article_count = 0
        
        for link in links:
            if company_article_count >= MAX_WEBSITE_ARTICLES:
                break # Stop scraping this source if we hit the limit
                
            data = extract_article_data(link)
            if data and data['date'] and start_date <= data['date'] <= end_date:
                data['company'] = source_name 
                collected_news.append(data)
                company_article_count += 1
                logging.info(f"  Found: {data['title']}")
    
    return collected_news

# =====================================================================
# SECTION 5: LINKEDIN SCRAPING VIA APIFY
# =====================================================================

def scrape_linkedin():
    start_date, end_date = get_date_range()
    logging.info(f"Scraping LinkedIn from {start_date} to {end_date}")
    
    client = ApifyClient(APIFY_API_TOKEN)
    run_input = {
        "targetUrls": LINKEDIN_COMPANIES,
        "maxResults": MAX_LINKEDIN_POSTS, # Limit per company
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
            # Clean up company name from URL
            company_name = company_url.split('/')[-2].replace('-', ' ').title() if company_url else "Unknown"
            
            collected_posts.append({
                'url': item.get('url', ''),
                'title': item.get('text', '')[:100] + "..." if len(item.get('text', '')) > 100 else item.get('text', ''),
                'date': post_date, # Kept as date object
                'text': item.get('text', '')[:500],
                'company': company_name,
                'source': 'linkedin'
            })
            logging.info(f"  Found LinkedIn post: {item.get('text', '')[:50]}")
    
    logging.info(f"Found {len(collected_posts)} LinkedIn posts in date range")
    return collected_posts

# =====================================================================
# SECTION 6: AI PROCESSING (CONCURRENT BATCHING)
# =====================================================================

def process_single_item(item):
    """Processes exactly ONE item with Groq to keep context tiny and output perfect."""
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
    """Uses ThreadPoolExecutor to process items concurrently but independently."""
    if not all_items:
        return []
    
    logging.info(f"Processing {len(all_items)} items with Groq concurrently...")
    processed_items = []
    
    # Run up to 5 concurrent LLM calls at a time to be fast but avoid rate limits
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_single_item, item): item for item in all_items}
        for future in concurrent.futures.as_completed(futures):
            processed_items.append(future.result())
            
    return processed_items

# =====================================================================
# SECTION 7: HTML TABLE EMAIL GENERATION
# =====================================================================

def generate_html_email(processed_items, start_date, end_date):
    # Group by Category instead of Company
    grouped = defaultdict(list)
    for item in processed_items:
        cat = item.get('category', 'General News')
        grouped[cat].append(item)
        
    # Define the order of categories
    category_order = ["New Development", "Progress/Update", "People Mentioned", "General News"]
    
    # Email Client Safe HTML Table Styles
    table_style = 'border="1" cellpadding="10" cellspacing="0" style="border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size: 14px; color: #333; border-color: #ddd;"'
    th_style = 'style="background-color: #f4f4f4; text-align: left; padding: 10px; border: 1px solid #ddd;"'
    td_style = 'style="padding: 10px; border: 1px solid #ddd; vertical-align: top;"'

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.5; color: #333; max-width: 800px; margin: 0 auto;">
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
            
        # Sort items by company name inside the table
        items.sort(key=lambda x: x['company'])
        
        html += f"<h2 style='color: #0066cc; font-size: 18px; border-bottom: 2px solid #0066cc; padding-bottom: 5px; margin-top: 30px;'>{category} ({len(items)})</h2>"
        html += f"<table {table_style}>"
        html += f"<tr><th {th_style}>Company</th><th {th_style}>Headline & Summary</th><th {th_style}>Link</th></tr>"
        
        for item in items:
            headline = item.get('headline', 'No Headline')
            summary = item.get('summary', 'No summary.')
            url = item.get('url', '#')
            company = item.get('company', 'Unknown')
            
            html += f"""
            <tr>
                <td {td_style} style="font-weight: bold; width: 15%; background-color: #fafafa;">{company}</td>
                <td {td_style} style="width: 65%;">
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
# SECTION 8: MAIN EXECUTION
# =====================================================================

def main():
    logging.info("Starting BW Group Weekly News Bot...")
    
    archive = load_archive()
    logging.info(f"Archive contains {len(archive)} previously sent items")
    
    website_news = scrape_websites()
    logging.info(f"Found {len(website_news)} website articles")
    
    linkedin_posts = scrape_linkedin()
    logging.info(f"Found {len(linkedin_posts)} LinkedIn posts")
    
    all_items = website_news + linkedin_posts
    
    new_items = [item for item in all_items if is_new_item(item['url'], archive)]
    logging.info(f"After deduplication: {len(new_items)} new items")
    
    if not new_items:
        logging.info("No new items to report")
        return
    
    # Process with Groq AI concurrently
    processed_items = process_with_groq_concurrent(new_items)
    
    # Generate and send email
    start_date, end_date = get_date_range()
    html_content = generate_html_email(processed_items, start_date, end_date)
    send_email(html_content, start_date, end_date)
    
    # Convert dates to text strings right before saving to JSON
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
