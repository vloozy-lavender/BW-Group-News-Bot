import os
import json
import logging
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from collections import defaultdict
from groq import Groq
import concurrent.futures

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# API Keys
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", "vernonlee3701@gmail.com")  # must match the Gmail account the app password belongs to

# Support multiple emails separated by commas
raw_email_to = os.getenv("EMAIL_TO", "vernonlee37@gmail.com")
EMAIL_TO = [email.strip() for email in raw_email_to.split(",")]

# GitHub repo info (to fetch archive.json)
GITHUB_REPO = "vloozy-lavender/BW-Group-News-Bot"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # Optional, for private repos

def load_archive_from_github():
    """Fetch archive.json directly from GitHub repository."""
    if GITHUB_TOKEN:
        # For private repos
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/archive.json"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        resp = requests.get(url, headers=headers)
    else:
        # For public repos
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/archive.json"
        resp = requests.get(url)
    
    if resp.status_code == 200:
        if GITHUB_TOKEN:
            # GitHub API returns base64 encoded content
            import base64
            content = resp.json().get('content', '')
            decoded = base64.b64decode(content).decode('utf-8')
            return json.loads(decoded)
        else:
            return resp.json()
    else:
        logging.error(f"Failed to fetch archive.json: {resp.status_code}")
        return []

def load_archive_from_local():
    """Load archive.json from local file system."""
    try:
        with open('archive.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logging.error("archive.json not found locally")
        return []

def filter_by_date_range(items, days_back=None):
    """Filter items by date range. If days_back is None, return all items."""
    if days_back is None:
        return items
    
    cutoff_date = datetime.now().date() - timedelta(days=days_back)
    filtered = []
    
    for item in items:
        try:
            item_date = datetime.strptime(item.get('date', ''), '%Y-%m-%d').date()
            if item_date >= cutoff_date:
                filtered.append(item)
        except:
            # If date parsing fails, include the item anyway
            filtered.append(item)
    
    return filtered

def process_single_item(item):
    """Processes exactly ONE item with Groq."""
    prompt = f"""You are a corporate news analyst. Analyze this single update from {item.get('company', 'Unknown')}.

Text: {item.get('text', '')[:500]}
Title: {item.get('title', 'No Title')}

Return a JSON object with exactly these fields:
- "headline": A short, punchy headline (max 10 words).
- "summary": Exactly 1-2 sentences summarizing the key point.
- "category": MUST be exactly one of these: "New Development", "Progress/Update", "People Mentioned", "General News".

Return ONLY the JSON object. No markdown."""

    client = Groq(api_key=GROQ_API_KEY)
    try:
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=600,
            reasoning_effort="low",
            response_format={"type": "json_object"}
        )
        parsed = json.loads(completion.choices[0].message.content)
        parsed['url'] = item.get('url', '#')
        parsed['company'] = item.get('company', 'Unknown')
        parsed['date'] = item.get('date', 'Unknown')
        return parsed
    except Exception as e:
        logging.error(f"Groq failed for {item.get('url')}: {e}")
        return {
            'headline': item.get('title', 'No Title'),
            'summary': 'Summary unavailable.',
            'category': 'General News',
            'url': item.get('url', '#'),
            'company': item.get('company', 'Unknown'),
            'date': item.get('date', 'Unknown')
        }

def process_with_groq_concurrent(all_items):
    """Uses ThreadPoolExecutor to process items concurrently."""
    if not all_items:
        return []
    
    logging.info(f"Processing {len(all_items)} items with Groq concurrently...")
    processed_items = []
    
    # Process in batches to avoid overwhelming the API
    batch_size = 10
    for i in range(0, len(all_items), batch_size):
        batch = all_items[i:i+batch_size]
        logging.info(f"Processing batch {i//batch_size + 1}/{(len(all_items) + batch_size - 1)//batch_size}")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_single_item, item): item for item in batch}
            for future in concurrent.futures.as_completed(futures):
                processed_items.append(future.result())
            
    return processed_items

def generate_html_email(processed_items, title_suffix=""):
    """Generate HTML email with tables."""
    grouped = defaultdict(list)
    for item in processed_items:
        cat = item.get('category', 'General News')
        grouped[cat].append(item)
        
    category_order = ["New Development", "Progress/Update", "People Mentioned", "General News"]
    
    table_style = 'border="1" cellpadding="10" cellspacing="0" style="border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size: 14px; color: #333; border-color: #ddd;"'
    th_style = 'style="background-color: #f4f4f4; text-align: left; padding: 10px; border: 1px solid #ddd;"'
    td_style = 'style="padding: 10px; border: 1px solid #ddd; vertical-align: top;"'

    # Calculate date range from items
    dates = []
    for item in processed_items:
        try:
            date_obj = datetime.strptime(item.get('date', ''), '%Y-%m-%d')
            dates.append(date_obj)
        except:
            pass
    
    if dates:
        start_date = min(dates)
        end_date = max(dates)
        date_range_str = f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"
    else:
        date_range_str = "Date range unavailable"

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.5; color: #333; max-width: 900px; margin: 0 auto;">
        <h1 style="color: #003366; font-size: 24px; margin-bottom: 5px;">BW Group Archive Recap {title_suffix}</h1>
        <p style="color: #666; font-size: 14px; margin-top: 0;">
            <strong>Date Range:</strong> {date_range_str}<br>
            <strong>Total Items:</strong> {len(processed_items)} items tracked across all subsidiaries.
        </p>
        <hr style="border: 0; border-top: 2px solid #003366; margin: 20px 0;">
    """

    for category in category_order:
        items = grouped.get(category, [])
        if not items:
            continue
            
        # Sort by date descending
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
        ARCHIVE RECAP - Generated by BW Group News Bot
    </p>
    </body>
    </html>
    """
    return html

def send_email(html_content, title_suffix=""):
    """Send the email via Gmail SMTP."""
    subject = f"ARCHIVE RECAP: BW Group News Digest {title_suffix}"

    # TEMPORARY DEBUG — remove once the 535 error is resolved.
    logging.info(f"DEBUG: logging into Gmail as '{EMAIL_FROM}' with an app password of length {len(GMAIL_APP_PASSWORD) if GMAIL_APP_PASSWORD else 0}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logging.info(f"Email sent successfully to {EMAIL_TO}")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Generate recap email from archive.json')
    parser.add_argument('--days', type=int, help='Only include items from the last X days')
    parser.add_argument('--local', action='store_true', help='Load archive.json from local file instead of GitHub')
    args = parser.parse_args()
    
    logging.info("Starting archive recap email generation...")
    
    # Load archive
    if args.local:
        logging.info("Loading archive.json from local file...")
        archive = load_archive_from_local()
    else:
        logging.info("Fetching archive.json from GitHub...")
        archive = load_archive_from_github()
    
    if not archive:
        logging.error("No items found in archive")
        return
    
    logging.info(f"Loaded {len(archive)} items from archive")
    
    # Filter by date range if specified
    if args.days:
        archive = filter_by_date_range(archive, args.days)
        logging.info(f"After filtering to last {args.days} days: {len(archive)} items")
    
    if not archive:
        logging.error("No items match the date filter")
        return
    
    # Process with AI
    processed_items = process_with_groq_concurrent(archive)
    
    # Generate title suffix
    if args.days:
        title_suffix = f"(Last {args.days} Days)"
    else:
        title_suffix = "(All Time)"
    
    # Generate and send email
    html_content = generate_html_email(processed_items, title_suffix)
    email_sent = send_email(html_content, title_suffix)

    if email_sent:
        logging.info("Archive recap email sent successfully!")
    else:
        logging.error("Archive recap email FAILED to send — see the error above.")
        raise SystemExit(1)  # make the GitHub Actions run show as failed, not a false green check

if __name__ == "__main__":
    main()
