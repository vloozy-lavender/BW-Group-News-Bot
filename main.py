import os
import json
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from urllib.parse import urljoin
from groq import Groq
from apify_client import ApifyClient
import resend
from collections import defaultdict
import urllib3

# Disable SSL warnings for problematic sites
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# SECTION 1: CONFIGURATION
# =====================================================================

# Companies to track (official newsroom URLs)
COMPANIES_TO_TRACK = {
    "BW Group": "https://www.bw-group.com/news",
    "BW Digital": "https://www.bw-digital.com/news/",
    "BW Dry Cargo": "https://bwdrycargo.com/news",
    "BW Energy": "https://www.bwenergy.no/en/news-and-media/",
    "BW ESS": "https://bw-ess.com/news",
    "BW Epic Kosan": "https://bwek.com/media/latest-news/",
    "BW Ideol": "https://www.bw-ideol.com/en/latest-news",
    "BW LNG": "https://bwlng.com/news",
    "BW LPG": "https://www.bwlpg.com/media/press-releases/",
    "BW Offshore": "https://bwoffshore.com/news-media",
    "BW Water": "https://bw-water.com/news/",
    "Cadeler": "https://www.cadeler.com/news",
    "Corvus Energy": "https://corvusenergy.com/news",
    "Hafnia": "https://hafnia.com/news/",
    "Navigator Gas": "https://navigatorgas.com/news/",
}

# LinkedIn company pages to track
LINKEDIN_COMPANIES = [
    "https://www.linkedin.com/company/bw-group/",
    "https://www.linkedin.com/company/hafnia/",
    "https://www.linkedin.com/company/bw-offshore/",
    "https://www.linkedin.com/company/bw-lng/",
    # Add more LinkedIn URLs here
]

# API Keys
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "news@yourdomain.com")  # Set this in Resend
EMAIL_TO = os.getenv("EMAIL_TO", "your-email@bw-group.com")

# Archive file path
ARCHIVE_FILE = "archive.json"

# =====================================================================
# SECTION 2: DATE LOGIC (Weekly: Last 7 days)
# =====================================================================

def get_date_range():
    """Returns the date range for the past week (Friday to Thursday)."""
    today = datetime.now().date()
    # Look back 7 days from today
    start_date = today - timedelta(days=7)
    end_date = today
    return start_date, end_date

# =====================================================================
# SECTION 3: ARCHIVE MANAGEMENT (Deduplication)
# =====================================================================

def load_archive():
    """Load the archive of previously sent news."""
    if os.path.exists(ARCHIVE_FILE):
        with open(ARCHIVE_FILE, 'r') as f:
            return json.load(f)
    return []

def save_archive(archive):
    """Save the updated archive."""
    with open(ARCHIVE_FILE, 'w') as f:
        json.dump(archive, f, indent=2)

def is_new_item(url, archive):
    """Check if this URL has already been sent."""
    return not any(item['url'] == url for item in archive)

# =====================================================================
# SECTION 4: WEBSITE SCRAPING
# =====================================================================

def get_article_links(main_url):
    """Finds all potential article links on a main news page."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(main_url, timeout=15, headers=headers, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        links = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg']):
                continue
            
            full_url = urljoin(main_url, href)
            if len(href.split('/')) > 2:
                links.add(full_url)
        return list(links)
    except Exception as e:
        logging.error(f"Failed to get links from {main_url}: {e}")
        return []

def extract_article_data(article_url):
    """Visits an article, extracts the date and the raw text."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(article_url, timeout=15, headers=headers, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Extract publication date
        pub_date = None
        meta_date = soup.find('meta', property='article:published_time')
        if meta_date and meta_date.get('content'):
            try:
                pub_date = date_parser.parse(meta_date['content']).date()
            except:
                pass
        
        if not pub_date:
            time_tag = soup.find('time')
            if time_tag and time_tag.get('datetime'):
                try:
                    pub_date = date_parser.parse(time_tag['datetime']).date()
                except:
                    pass

        # Extract title
        title = soup.title.string.strip() if soup.title else "No Title"
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content'].strip()
        
        # Extract text (first 500 chars for AI)
        for element in soup(["script", "style", "nav", "header", "footer"]):
            element.extract()
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
        
        return {
            'url': article_url,
            'title': title,
            'date': pub_date,
            'text': text[:500],
            'source': 'website'
        }
    except Exception as e:
        logging.error(f"Failed to process {article_url}: {e}")
        return None

def scrape_websites():
    """Scrape all official company websites."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping websites from {start_date} to {end_date}")
    
    collected_news = []
    
    for company_name, news_url in COMPANIES_TO_TRACK.items():
        logging.info(f"Scanning {company_name}...")
        links = get_article_links(news_url)
        
        for link in links:
            data = extract_article_data(link)
            if data and data['date'] and start_date <= data['date'] <= end_date:
                data['company'] = company_name
                collected_news.append(data)
                logging.info(f"  Found: {data['title']}")
    
    return collected_news

# =====================================================================
# SECTION 5: LINKEDIN SCRAPING VIA APIFY
# =====================================================================

def scrape_linkedin():
    """Scrape LinkedIn company pages using Apify."""
    start_date, end_date = get_date_range()
    logging.info(f"Scraping LinkedIn from {start_date} to {end_date}")
    
    client = ApifyClient(APIFY_API_TOKEN)
    
    # Prepare input for the LinkedIn scraper
    run_input = {
        "targetUrls": LINKEDIN_COMPANIES,
        "maxPosts": 20,  # Get last 20 posts per company
        "includeQuotePosts": False,
        "includeReposts": False,
    }
    
    # Run the scraper
    logging.info("Starting Apify LinkedIn scraper...")
    run = client.actor("harvestapi/linkedin-company-posts").call(run_input=run_input)
    
    # Fetch results
    collected_posts = []
    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        # Parse the post date
        try:
            post_date = date_parser.parse(item.get('date', '')).date()
        except:
            continue
        
        # Filter by date range
        if start_date <= post_date <= end_date:
            # Extract company name from URL
            company_url = item.get('companyUrl', '')
            company_name = company_url.split('/')[-2] if company_url else "Unknown"
            
            collected_posts.append({
                'url': item.get('url', ''),
                'title': item.get('text', '')[:100] + "..." if len(item.get('text', '')) > 100 else item.get('text', ''),
                'date': post_date,
                'text': item.get('text', '')[:500],
                'company': company_name.title(),
                'source': 'linkedin'
            })
            logging.info(f"  Found LinkedIn post: {item.get('text', '')[:50]}")
    
    logging.info(f"Found {len(collected_posts)} LinkedIn posts in date range")
    return collected_posts

# =====================================================================
# SECTION 6: AI PROCESSING WITH GROQ
# =====================================================================

def process_with_groq(all_items):
    """Use Groq to categorize and summarize all items."""
    if not all_items:
        return []
    
    # Build prompt for Groq
    prompt = """You are a corporate news analyst. I will give you a list of news articles and LinkedIn posts from BW Group companies.

For each item, return a JSON object with these exact fields:
- "company": The company name
- "headline": The original headline (or first 50 chars of text for LinkedIn)
- "summary": Exactly 1-2 sentences summarizing the key point
- "category": One of these exact values: "Press Release", "Award", "Partnership", "Sustainability", "People", "Event", "Milestone", "Other"
- "url": The original URL

Return ONLY a JSON array of objects. No markdown, no explanation, just the JSON array.

Items to process:
"""
    
    for i, item in enumerate(all_items):
        prompt += f"\n--- Item {i+1} ---\n"
        prompt += f"Company: {item['company']}\n"
        prompt += f"Title: {item['title']}\n"
        prompt += f"Text: {item['text']}\n"
        prompt += f"URL: {item['url']}\n"
        prompt += f"Source: {item['source']}\n"
    
    client = Groq(api_key=GROQ_API_KEY)
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )
        
        # Parse the JSON response
        response_text = completion.choices[0].message.content
        # Groq sometimes wraps JSON in {"content": [...]} or returns raw array
        if response_text.startswith('['):
            processed_items = json.loads(response_text)
        else:
            data = json.loads(response_text)
            processed_items = data.get('items', data.get('content', []))
        
        # Merge with original URLs
        for i, item in enumerate(processed_items):
            if i < len(all_items):
                item['url'] = all_items[i]['url']
                item['company'] = all_items[i]['company']
                item['date'] = str(all_items[i]['date'])
        
        return processed_items
    except Exception as e:
        logging.error(f"Groq processing failed: {e}")
        # Fallback: return items without AI processing
        return [{
            'company': item['company'],
            'headline': item['title'],
            'summary': 'Summary unavailable',
            'category': 'Other',
            'url': item['url'],
            'date': str(item['date'])
        } for item in all_items]

# =====================================================================
# SECTION 7: EMAIL GENERATION & DELIVERY
# =====================================================================

def generate_html_email(processed_items, start_date, end_date):
    """Generate a clean HTML email grouped by company."""
    
    # Group by company
    grouped = defaultdict(list)
    for item in processed_items:
        grouped[item['company']].append(item)
    
    # Build HTML
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <h2 style="color: #003366;">BW Group Weekly News Digest</h2>
        <p><strong>Period:</strong> {start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}</p>
        <p><strong>Total Updates:</strong> {len(processed_items)} items</p>
        <hr style="border: 1px solid #ddd; margin: 20px 0;">
    """
    
    # Sort companies alphabetically
    for company in sorted(grouped.keys()):
        items = grouped[company]
        html += f"<h3 style='color: #0066cc; margin-top: 30px;'>{company} ({len(items)} updates)</h3>"
        
        for item in items:
            category_color = {
                'Press Release': '#28a745',
                'Award': '#ffc107',
                'Partnership': '#17a2b8',
                'Sustainability': '#20c997',
                'People': '#6f42c1',
                'Event': '#fd7e14',
                'Milestone': '#e83e8c',
                'Other': '#6c757d'
            }.get(item['category'], '#6c757d')
            
            html += f"""
            <div style="margin-bottom: 20px; padding: 15px; background: #f8f9fa; border-left: 3px solid {category_color};">
                <span style="display: inline-block; padding: 2px 8px; background: {category_color}; color: white; font-size: 11px; border-radius: 3px; margin-bottom: 5px;">
                    {item['category']}
                </span>
                <h4 style="margin: 5px 0;">{item['headline']}</h4>
                <p style="margin: 5px 0; color: #555;">{item['summary']}</p>
                <a href="{item['url']}" style="color: #0066cc; text-decoration: none; font-size: 13px;">Read Original →</a>
            </div>
            """
    
    html += """
    </body>
    </html>
    """
    
    return html

def send_email(html_content, start_date, end_date):
    """Send the email via Resend."""
    resend.api_key = RESEND_API_KEY
    
    subject = f"BW Group Weekly News Digest ({start_date.strftime('%b %d')} - {end_date.strftime('%b %d')})"
    
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
    
    # Load archive
    archive = load_archive()
    logging.info(f"Archive contains {len(archive)} previously sent items")
    
    # Scrape websites
    website_news = scrape_websites()
    logging.info(f"Found {len(website_news)} website articles")
    
    # Scrape LinkedIn
    linkedin_posts = scrape_linkedin()
    logging.info(f"Found {len(linkedin_posts)} LinkedIn posts")
    
    # Combine all items
    all_items = website_news + linkedin_posts
    
    # Filter out duplicates (already in archive)
    new_items = [item for item in all_items if is_new_item(item['url'], archive)]
    logging.info(f"After deduplication: {len(new_items)} new items")
    
    if not new_items:
        logging.info("No new items to report")
        return
    
    # Process with Groq AI
    processed_items = process_with_groq(new_items)
    
    # Generate and send email
    start_date, end_date = get_date_range()
    html_content = generate_html_email(processed_items, start_date, end_date)
    send_email(html_content, start_date, end_date)
    
    # Update archive
    archive.extend(new_items)
    save_archive(archive)
    logging.info(f"Archive updated. Total items: {len(archive)}")
    
    logging.info("Bot finished successfully")

if __name__ == "__main__":
    main()
