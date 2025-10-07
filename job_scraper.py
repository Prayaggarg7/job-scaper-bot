import os
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import sqlite3
import re
from urllib.parse import quote_plus
from flask import Flask, render_template

# Paths for Fly.io persistent volume
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "jobs.db")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")

# Logging helper
def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")

# Flask app for dashboard
app = Flask(__name__)

# Bot class
class JobScraperBot:
    def __init__(self):
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.skills = os.getenv('JOB_SKILLS', 'python,javascript,react').lower().split(',')
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))  # 5 min
        self.max_days_old = int(os.getenv('MAX_DAYS_OLD', '10'))
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
        }
        self.init_database()

    def init_database(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                link TEXT,
                portal TEXT,
                posted_date TEXT,
                days_ago INTEGER,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def generate_job_id(self, title, company, link):
        return hashlib.md5(f"{title}{company}{link}".encode()).hexdigest()

    def is_job_seen(self, job_id):
        self.cursor.execute('SELECT job_id FROM jobs WHERE job_id = ?', (job_id,))
        return self.cursor.fetchone() is not None

    def mark_job_seen(self, job):
        try:
            self.cursor.execute('''
                INSERT INTO jobs (job_id,title,company,link,portal,posted_date,days_ago)
                VALUES (?,?,?,?,?,?,?)
            ''', (job['id'], job['title'], job['company'], job['link'], job['portal'], job['posted_date'], job['days_ago']))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def matches_skills(self, text):
        text_lower = text.lower()
        return any(skill.strip() in text_lower for skill in self.skills)

    def parse_days_ago(self, date_string):
        if not date_string:
            return 999
        date_lower = date_string.lower()
        if any(w in date_lower for w in ['today','just now','minutes ago','hours ago']):
            return 0
        if 'yesterday' in date_lower:
            return 1
        days_match = re.search(r'(\d+)\s*day', date_lower)
        if days_match:
            return int(days_match.group(1))
        weeks_match = re.search(r'(\d+)\s*week', date_lower)
        if weeks_match:
            return int(weeks_match.group(1))*7
        months_match = re.search(r'(\d+)\s*month', date_lower)
        if months_match:
            return int(months_match.group(1))*30
        return 999

    def is_recent_job(self, days_ago):
        return days_ago <= self.max_days_old

    # ---- Scrapers ----
    def scrape_remotive(self):
        jobs=[]
        try:
            url="https://remotive.com/api/remote-jobs?limit=50"
            r=requests.get(url,headers=self.headers,timeout=15)
            if r.status_code==200:
                data=r.json()
                for j in data.get('jobs',[]):
                    pub_date=j.get('publication_date','')
                    try:
                        job_date=datetime.strptime(pub_date,'%Y-%m-%dT%H:%M:%S')
                        days_ago=(datetime.now()-job_date).days
                    except:
                        days_ago=999
                    if not self.is_recent_job(days_ago):
                        continue
                    text=f"{j.get('title','')} {j.get('description','')} {' '.join(j.get('tags',[]))}"
                    if self.matches_skills(text):
                        jobs.append({'id':self.generate_job_id(j.get('title',''),j.get('company_name',''),j.get('url','')),
                                     'title':j.get('title','N/A'),
                                     'company':j.get('company_name','N/A'),
                                     'link':j.get('url',''),
                                     'portal':'Remotive',
                                     'posted_date':pub_date,
                                     'days_ago':days_ago})
        except Exception as e:
            log(f"❌ Remotive error: {e}")
        return jobs

    def scrape_indeed(self):
        jobs=[]
        try:
            query=' OR '.join(self.skills[:3])
            url=f"https://www.indeed.com/rss?q={quote_plus(query)}&fromage={self.max_days_old}"
            r=requests.get(url,headers=self.headers,timeout=15)
            if r.status_code==200:
                soup=BeautifulSoup(r.content,'xml')
                for item in soup.find_all('item')[:30]:
                    title=item.find('title').text if item.find('title') else 'N/A'
                    link=item.find('link').text if item.find('link') else ''
                    desc=item.find('description').text if item.find('description') else ''
                    pub=item.find('pubDate').text if item.find('pubDate') else ''
                    try:
                        job_date=datetime.strptime(pub,'%a, %d %b %Y %H:%M:%S %Z')
                        days_ago=(datetime.now()-job_date).days
                    except:
                        days_ago=0
                    if not self.is_recent_job(days_ago):
                        continue
                    company='Various'
                    if ' - ' in title:
                        parts=title.split(' - ')
                        if len(parts)>1:
                            company=parts[-1]
                    text=f"{title} {desc}"
                    if self.matches_skills(text):
                        jobs.append({'id':self.generate_job_id(title,company,link),
                                     'title':title,'company':company,'link':link,'portal':'Indeed',
                                     'posted_date':pub,'days_ago':days_ago})
        except Exception as e:
            log(f"❌ Indeed error: {e}")
        return jobs

    def scrape_remoteok(self):
        jobs=[]
        try:
            url="https://remoteok.com/api"
            r=requests.get(url,headers=self.headers,timeout=15)
            if r.status_code==200:
                data=r.json()
                for j in data[1:31]:
                    if not isinstance(j,dict):
                        continue
                    epoch=j.get('epoch',0)
                    if epoch:
                        job_date=datetime.fromtimestamp(epoch)
                        days_ago=(datetime.now()-job_date).days
                    else:
                        days_ago=0
                    if not self.is_recent_job(days_ago):
                        continue
                    text=f"{j.get('position','')} {j.get('description','')} {' '.join(j.get('tags',[]))}"
                    if self.matches_skills(text):
                        jobs.append({'id':self.generate_job_id(j.get('position',''),j.get('company',''),j.get('url','')),
                                     'title':j.get('position','N/A'),'company':j.get('company','N/A'),
                                     'link':j.get('url',''),'portal':'RemoteOK','posted_date':j.get('date','N/A'),'days_ago':days_ago})
        except Exception as e:
            log(f"❌ RemoteOK error: {e}")
        return jobs

    # Combine all portals
    def scrape_all(self):
        all_jobs=[]
        portals=[self.scrape_remotive,self.scrape_indeed,self.scrape_remoteok]
        for fn in portals:
            try:
                log(f"🔍 Scraping {fn.__name__}...")
                jobs=fn()
                all_jobs.extend(jobs)
                log(f"   ✓ Found {len(jobs)} matching jobs")
            except Exception as e:
                log(f"   ✗ Error: {e}")
            time.sleep(2)
        return all_jobs

    # Optional Telegram
    def send_telegram(self, job):
        if not self.telegram_bot_token or not self.telegram_chat_id:
            return
        msg=f"New Job: {job['title']} at {job['company']} ({job['portal']})\nApply: {job['link']}"
        url=f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        try:
            r=requests.post(url,json={'chat_id':self.telegram_chat_id,'text':msg,'disable_web_page_preview':False})
            if r.status_code==200:
                log(f"✅ Telegram sent: {job['title']}")
            else:
                log(f"❌ Telegram error: {r.status_code}")
        except Exception as e:
            log(f"❌ Telegram send fail: {e}")

    # Process jobs
    def run_scraper_cycle(self):
        jobs=self.scrape_all()
        new_count=0
        for job in jobs:
            if not self.is_job_seen(job['id']):
                self.mark_job_seen(job)
                self.send_telegram(job)
                new_count+=1
        log(f"📊 Summary: {len(jobs)} total, {new_count} new")

    # Main loop
    def run(self):
        log("🤖 Job Scraper Bot Started")
        while True:
            try:
                self.run_scraper_cycle()
                time.sleep(self.check_interval)
            except KeyboardInterrupt:
                log("👋 Bot stopped by user")
                break
            except Exception as e:
                log(f"❌ Main loop error: {e}")
                time.sleep(60)

bot = JobScraperBot()

# Flask route for dashboard
@app.route("/")
def dashboard():
    cursor=bot.conn.cursor()
    cursor.execute("SELECT title,company,link,portal,posted_date,days_ago FROM jobs ORDER BY posted_date DESC")
    rows=cursor.fetchall()
    jobs=[{'title':r[0],'company':r[1],'link':r[2],'portal':r[3],'posted_date':r[4],'days_ago':r[5]} for r in rows]
    return render_template("index.html",jobs=jobs)

if __name__=="__main__":
    # Run scraper in a separate thread
    import threading
    t=threading.Thread(target=bot.run)
    t.daemon=True
    t.start()
    # Run Flask dashboard
    app.run(host="0.0.0.0",port=8080)
