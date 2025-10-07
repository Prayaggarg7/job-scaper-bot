import os
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import sqlite3
import re
from urllib.parse import quote_plus
from flask import Flask, render_template, request, Response

# ---------------- Logging helper ----------------
def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {msg}", flush=True)

# ---------------- Bot Class ----------------
class JobScraperBot:
    def __init__(self):
        self.telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
        self.skills = os.getenv('JOB_SKILLS', 'python,javascript,react').lower().split(',')
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))  # default 5 min
        self.max_days_old = int(os.getenv('MAX_DAYS_OLD', '10'))
        self.dash_user = os.getenv('DASHBOARD_USER', 'admin')
        self.dash_pass = os.getenv('DASHBOARD_PASS', 'password')

        self.headers = {'User-Agent': 'Mozilla/5.0'}

        # DB setup
        self.init_database()

    def init_database(self):
        self.conn = sqlite3.connect('jobs.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS seen_jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                url TEXT,
                portal TEXT,
                posted_date TEXT,
                days_ago INTEGER,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    # ---------- Job helpers ----------
    def generate_job_id(self, title, company, url):
        return hashlib.md5(f"{title}{company}{url}".encode()).hexdigest()

    def is_job_seen(self, job_id):
        self.cursor.execute('SELECT job_id FROM seen_jobs WHERE job_id=?', (job_id,))
        return self.cursor.fetchone() is not None

    def mark_job_seen(self, job_id, title, company, url, portal, posted_date, days_ago):
        try:
            self.cursor.execute('''
                INSERT INTO seen_jobs (job_id,title,company,url,portal,posted_date,days_ago)
                VALUES (?,?,?,?,?,?,?)
            ''', (job_id, title, company, url, portal, posted_date, days_ago))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def matches_skills(self, job_text):
        job_text_lower = job_text.lower()
        return any(skill.strip() in job_text_lower for skill in self.skills)

    def parse_days_ago(self, date_str):
        if not date_str:
            return 999
        date_lower = date_str.lower()
        if any(w in date_lower for w in ['today','just now','minutes ago','hours ago','hour ago']):
            return 0
        if 'yesterday' in date_lower:
            return 1
        days_match = re.search(r'(\d+)\s*day', date_lower)
        if days_match:
            return int(days_match.group(1))
        weeks_match = re.search(r'(\d+)\s*week', date_lower)
        if weeks_match:
            return int(weeks_match.group(1)) * 7
        months_match = re.search(r'(\d+)\s*month', date_lower)
        if months_match:
            return int(months_match.group(1)) * 30
        return 999

    def is_recent_job(self, days_ago):
        return days_ago <= self.max_days_old

    # ---------- Scrapers ----------
    def scrape_remotive(self):
        jobs = []
        url = "https://remotive.com/api/remote-jobs?limit=50"
        log("🔍 Scraping Remotive jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                for job in data.get('jobs', []):
                    pub_date = job.get('publication_date','')
                    try:
                        job_date = datetime.strptime(pub_date, '%Y-%m-%dT%H:%M:%S')
                        days_ago = (datetime.now() - job_date).days
                    except:
                        days_ago = 999
                    if not self.is_recent_job(days_ago):
                        continue
                    job_text = f"{job.get('title','')} {job.get('description','')} {' '.join(job.get('tags',[]))}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': job.get('title','N/A'),
                            'company': job.get('company_name','N/A'),
                            'link': job.get('url',''),
                            'portal': 'Remotive',
                            'posted_date': pub_date,
                            'days_ago': days_ago
                        })
            log(f"✅ Found {len(jobs)} jobs on Remotive")
        except Exception as e:
            log(f"❌ Remotive scrape error: {e}")
        return jobs

    def scrape_indeed(self):
        jobs=[]
        query=' OR '.join(self.skills[:3])
        url=f"https://www.indeed.com/rss?q={quote_plus(query)}&fromage=10"
        log("🔍 Scraping Indeed RSS jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code==200:
                soup=BeautifulSoup(r.content,'xml')
                items=soup.find_all('item')[:30]
                for item in items:
                    title=item.find('title').text if item.find('title') else 'N/A'
                    link=item.find('link').text if item.find('link') else ''
                    desc=item.find('description').text if item.find('description') else ''
                    pub_date=item.find('pubDate').text if item.find('pubDate') else ''
                    try:
                        job_date=datetime.strptime(pub_date,'%a, %d %b %Y %H:%M:%S %Z')
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
                    job_text=f"{title} {desc}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': title,
                            'company': company,
                            'link': link,
                            'portal': 'Indeed',
                            'posted_date': pub_date,
                            'days_ago': days_ago
                        })
            log(f"✅ Found {len(jobs)} jobs on Indeed")
        except Exception as e:
            log(f"❌ Indeed scrape error: {e}")
        return jobs

    # You can add more portal scrapers here (RemoteOK, Naukri, Shine, TimesJobs, Glassdoor)

    def scrape_all_portals(self):
        all_jobs=[]
        for scraper in [self.scrape_remotive, self.scrape_indeed]:
            try:
                jobs=scraper()
                all_jobs.extend(jobs)
            except Exception as e:
                log(f"❌ Scraper exception: {e}")
            time.sleep(2)
        return all_jobs

    def process_jobs(self):
        jobs=self.scrape_all_portals()
        jobs.sort(key=lambda x:x['days_ago'])
        new_count=0
        for job in jobs:
            job_id=self.generate_job_id(job['title'],job.get('company',''),job['link'])
            if not self.is_job_seen(job_id):
                if self.telegram_bot_token and self.telegram_chat_id:
                    self.send_telegram(job)
                self.mark_job_seen(job_id,job['title'],job.get('company',''),job['link'],job['portal'],job['posted_date'],job['days_ago'])
                new_count+=1
        log(f"📊 Total jobs: {len(jobs)}, New: {new_count}")
        return jobs

    def send_telegram(self, job):
        msg=f"New Job: {job['title']} at {job.get('company','N/A')} [{job['portal']}] {job['link']}"
        url=f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload={'chat_id':self.telegram_chat_id,'text':msg}
        try:
            r=requests.post(url,json=payload,timeout=10)
            log(f"Telegram response: {r.status_code}")
        except Exception as e:
            log(f"❌ Telegram error: {e}")

# ---------------- Flask Dashboard ----------------
app=Flask(__name__)
bot=JobScraperBot()

# Simple auth
def check_auth(username,password):
    return username==bot.dash_user and password==bot.dash_pass

def authenticate():
    return Response('Login required', 401, {'WWW-Authenticate':'Basic realm="Login"'})

@app.route("/")
def index():
    auth=request.authorization
    if not auth or not check_auth(auth.username,auth.password):
        return authenticate()
    jobs=bot.process_jobs()
    return render_template('index.html',jobs=jobs)

if __name__=="__main__":
    log("🤖 Job Scraper & Dashboard starting...")
    app.run(host="0.0.0.0", port=int(os.getenv('PORT',8080)))
