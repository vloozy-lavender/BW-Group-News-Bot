import os
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from urllib.parse import urljoin
from groq import Groq
import telebot

# Configure basic logging for GitHub Actions debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# =====================================================================
# SECTION 1: CONFIGURATION (TWEAK THESE SETTINGS)
# =====================================================================

# 1. COMPANIES TO TRACK
# Add the company name and their specific news/press release URL here.
# The bot will use the exact name you type here in the final message.
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
    # "Company Name": "https://url-to-their-news-page.com",
}

# 2. API KEYS & TELEGRAM SETTINGS
# These are pulled from GitHub Secrets. Do not hardcode them here.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # Must be the Group Chat ID (starts with a minus sign, e.g., -100123456)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# 3. TIME SETTINGS
# By default, looks at yesterday. If it's Monday, looks at Fri/Sat/Sun.
def get_target_dates():
    today = datetime.now().date()
    if today.weekday() == 0:  # 0 = Monday
        return [today - timedelta(days=i) for i in range(3, 0, -1)] 
    else:
        return [today - timedelta(days=1)] 

# =====================================================================
# SECTION 2: SCRAPING ENGINE (TWEAK IF A SPECIFIC SITE FAILS)
# =====================================================================

def get_article_links(main_url):
    """Finds all potential article links on a main news page."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(main_url, timeout=15, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        links = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Ignore social links, emails, and downloads
            if any(x in href.lower() for x in ['facebook', 'twitter', 'linkedin', 'mailto:', '#', '.pdf', '.jpg']):
                continue
            
            full_url = urljoin(main_url, href)
            # Heuristic: News links usually have a deep path (e.g., /news/2024/my-article)
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
        resp = requests.get(article_url, timeout=15, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # --- TWEAK DATE EXTRACTION HERE IF NEEDED ---
        # Most modern sites use <time> tags or OpenGraph meta tags.
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

        # Extract Title and Text
        title = soup.title.string.strip() if soup.title else "No Title"
        
        # Remove boilerplate (scripts, navs) to get clean text
        for element in soup(["script", "style", "nav", "header", "footer"]):
            element.extract()
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 20])
        
        return {'url': article_url, 'title': title, 'date': pub_date, 'text': text[:2000]}
    except Exception as e:
        logging.error(f"Failed to process {article_url}: {e}")
        return None

def scrape_all_news():
    """Main loop: goes through all companies, scrapes, and filters by date."""
    target_dates = get_target_dates()
    logging.info(f"Looking for news on dates: {target_dates}")
    
    collected_news = []
    
    for company_name, news_url in COMPANIES_TO_TRACK.items():
        logging.info(f"Scanning {company_name}...")
        links = get_article_links(news_url)
        
        for link in links:
            data = extract_article_data(link)
            # Only keep articles that match our target dates
            if data and data['date'] in target_dates:
                data['company'] = company_name # Attach company name
                collected_news.append(data)
                logging.info(f"  Found: {data['title']}")
                
    return collected_news

# =====================================================================
# SECTION 3: LLM SUMMARIZATION (GROQ)
# =====================================================================

def format_with_groq(articles):
    """Uses Groq to turn raw text into simple, one-line summaries."""
    if not articles:
        return "No new updates from BW Group companies today."

    # Build a prompt for Groq
    prompt = "You are a news assistant. I will give you a list of news articles. For each article, write exactly ONE simple sentence summarizing it. Do not use bold text, do not use emojis, and do not use introductory filler words. Just give me the raw facts.\n\n"
    
    for i, art in enumerate(articles):
        prompt += f"Article {i+1} Title: {art['title']}\nArticle {i+1} Text: {art['text'][:500]}\n\n"
        
    client = Groq(api_key=GROQ_API_KEY)
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant", # Very fast and cheap/free tier friendly
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=800,
        )
        summaries = completion.choices[0].message.content.strip().split('\n')
        
        # Clean up the summaries (remove numbering if Groq adds it)
        clean_summaries = []
        for s in summaries:
            s = s.strip()
            if s:
                # Remove leading numbers like "1. " or "- "
                if len(s) > 2 and s[0].isdigit() and s[1] in '.-':
                    s = s[2:].strip()
                clean_summaries.append(s)
                
        return clean_summaries
    except Exception as e:
        logging.error(f"Groq failed: {e}. Falling back to raw titles.")
        return [art['title'] for art in articles]

# =====================================================================
# SECTION 4: TELEGRAM DELIVERY
# =====================================================================

def send_to_telegram(articles, summaries):
    """Formats the final plain-text message and sends it to the group."""
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
    
    # --- TWEAK MESSAGE FORMAT HERE ---
    # This is where you control exactly how the text looks in Telegram.
    message_lines = []
    message_lines.append(f"BW Group Daily News Update - {datetime.now().strftime('%Y-%m-%d')}")
    message_lines.append("-" * 30)
    
    for art, summary in zip(articles, summaries):
        # Format: Company Name: Summary - Link
        line = f"{art['company']}: {summary} - {art['url']}"
        message_lines.append(line)
        
    final_message = "\n".join(message_lines)
    
    # Send message (No parse_mode means it sends as raw, plain text)
    try:
        # Telegram has a 4096 char limit. We split if it's too long.
        if len(final_message) > 4000:
            for i in range(0, len(final_message), 4000):
                bot.send_message(TELEGRAM_CHAT_ID, final_message[i:i+4000])
        else:
            bot.send_message(TELEGRAM_CHAT_ID, final_message)
        logging.info("Successfully sent message to Telegram group.")
    except Exception as e:
        logging.error(f"Failed to send to Telegram: {e}")

# =====================================================================
# MAIN EXECUTION
# =====================================================================

def main():
    logging.info("Starting BW Group News Bot...")
    articles = scrape_all_news()
    logging.info(f"Total articles found for target dates: {len(articles)}")
    
    if articles:
        summaries = format_with_groq(articles)
        send_to_telegram(articles, summaries)
    else:
        # Send a quick message even if there's no news, so you know the bot ran
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        bot.send_message(TELEGRAM_CHAT_ID, f"BW Group Daily News Update - {datetime.now().strftime('%Y-%m-%d')}\n\nNo new updates found today.")
        
    logging.info("Bot finished.")

if __name__ == "__main__":
    main()
