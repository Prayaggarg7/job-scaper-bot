import os
import time
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import sqlite3
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
        self.skills = os.getenv(
            'JOB_SKILLS',
            'java,spring,spring boot,microservices,hibernate,jpa,rest api,sql,mysql,postgres,docker,kubernetes'
        ).lower().split(',')
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))
        self.max_days_old = int(os.getenv('MAX_DAYS_OLD', '10'))
        self.dash_user = os.getenv('DASHBOARD_USER', 'admin')
        self.dash_pass = os.getenv('DASHBOARD_PASS', 'password')
        self.headers = {'User-Agent': 'Mozilla/5.0'}

        self.init_database()

    # ---------------- Database ----------------
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

    # ---------------- Helpers ----------------
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

    # ---------------- Scrapers ----------------
    # Remotive
    def scrape_remotive(self):
        jobs = []
        url = "https://remotive.com/api/remote-jobs?limit=50"
        log("🔍 Scraping Remotive jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
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
        except Exception as e:
            log(f"❌ Remotive scrape error: {e}")
        return jobs

    # LinkedIn
    def scrape_linkedin(self):
        jobs = []
        query = quote_plus(' OR '.join(self.skills[:3]))
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={query}&location=Worldwide&f_TPR=r86400&start=0"
        log("🔍 Scraping LinkedIn jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('li')[:20]
                for card in job_cards:
                    try:
                        title_elem = card.find('h3', class_='base-search-card__title')
                        company_elem = card.find('h4', class_='base-search-card__subtitle')
                        link_elem = card.find('a', class_='base-card__full-link')
                        if title_elem and link_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = link_elem.get('href', '')
                            days_ago = 1
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'LinkedIn',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ LinkedIn scrape error: {e}")
        return jobs

    # Glassdoor
    def scrape_glassdoor(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:2]))
        url = f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}&fromAge=10"
        log("🔍 Scraping Glassdoor jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('li', class_='react-job-listing')[:20]
                for card in job_cards:
                    try:
                        title_elem = card.find('a', {'data-test': 'job-link'})
                        company_elem = card.find('div', class_='d-flex justify-content-between align-items-start')
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://www.glassdoor.com" + title_elem.get('href', '') if title_elem.get('href') else ''
                            days_ago = 3
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'Glassdoor',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ Glassdoor scrape error: {e}")
        return jobs

    # GitHub Jobs
    def scrape_github(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://jobs.github.com/positions.json?description={query}&full_time=true"
        log("🔍 Scraping GitHub Jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                for job in data[:20]:
                    created_at = job.get('created_at', '')
                    try:
                        job_date = datetime.strptime(created_at, '%a %b %d %H:%M:%S %Z %Y')
                        days_ago = (datetime.now() - job_date).days
                    except:
                        days_ago = 999
                    if not self.is_recent_job(days_ago):
                        continue
                    job_text = f"{job.get('title','')} {job.get('description','')} {job.get('company','')}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': job.get('title', 'N/A'),
                            'company': job.get('company', 'N/A'),
                            'link': job.get('url', ''),
                            'portal': 'GitHub Jobs',
                            'posted_date': created_at,
                            'days_ago': days_ago
                        })
        except Exception as e:
            log(f"❌ GitHub Jobs scrape error: {e}")
        return jobs

    # AngelList
    def scrape_angelco(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://angel.co/jobs?filter={query}"
        log("🔍 Scraping AngelList jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_listings = soup.find_all('div', class_='styles_role__xb3g6')[:15]
                for job in job_listings:
                    try:
                        title_elem = job.find('div', class_='styles_title__rbj3g')
                        company_elem = job.find('div', class_='styles_subtitle__q4dod')
                        link_elem = job.find('a')
                        if title_elem and link_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://angel.co" + link_elem.get('href', '') if link_elem.get('href') else ''
                            days_ago = 2
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'AngelList',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ AngelList scrape error: {e}")
        return jobs

    # Monster
    def scrape_monster(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.monster.com/jobs/search/?q={query}&where=remote&fromage=10"
        log("🔍 Scraping Monster jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('section', class_='card-content')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('h2', class_='title')
                        company_elem = card.find('div', class_='company')
                        link_elem = card.find('a')
                        if title_elem and link_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = link_elem.get('href', '')
                            days_ago = 4
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'Monster',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ Monster scrape error: {e}")
        return jobs

    # Dice
    def scrape_dice(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.dice.com/jobs?q={query}&countryCode=US&radius=30&radiusUnit=mi&page=1&pageSize=20&filters.remote=true&language=en"
        log("🔍 Scraping Dice jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('dhi-search-card')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('a', class_='card-title-link')
                        company_elem = card.find('a', class_='ng-star-inserted')
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = title_elem.get('href', '')
                            days_ago = 3
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'Dice',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ Dice scrape error: {e}")
        return jobs

    # FlexJobs
    def scrape_flexjobs(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.flexjobs.com/search?search={query}"
        log("🔍 Scraping FlexJobs jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_listings = soup.find_all('div', class_='job-list-item')[:15]
                for job in job_listings:
                    try:
                        title_elem = job.find('a', class_='job-title')
                        company_elem = job.find('div', class_='job-company')
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://www.flexjobs.com" + title_elem.get('href', '') if title_elem.get('href') else ''
                            days_ago = 2
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'FlexJobs',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except:
                        continue
        except Exception as e:
            log(f"❌ FlexJobs scrape error: {e}")
        return jobs

    # We Work Remotely
    def scrape_weworkremotely(self):
        jobs = []
        url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        log("🔍 Scraping We Work Remotely jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'xml')
                items = soup.find_all('item')[:20]
                for item in items:
                    title = item.find('title').text if item.find('title') else 'N/A'
                    link = item.find('link').text if item.find('link') else ''
                    pub_date = item.find('pubDate').text if item.find('pubDate') else ''
                    try:
                        job_date = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
                        days_ago = (datetime.now() - job_date).days
                    except:
                        days_ago = 0
                    if not self.is_recent_job(days_ago):
                        continue
                    company = 'N/A'
                    if ' - ' in title:
                        company = title.split(' - ')[0]
                    job_text = f"{title}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': title,
                            'company': company,
                            'link': link,
                            'portal': 'We Work Remotely',
                            'posted_date': pub_date,
                            'days_ago': days_ago
                        })
        except Exception as e:
            log(f"❌ We Work Remotely scrape error: {e}")
        return jobs

    # Add other scrapers (Jobserve, CareerBuilder, SimplyHired, ZipRecruiter)...
    # For brevity, these follow the same pattern as above.

    # ---------------- Aggregate ----------------
    def process_jobs(self):
        all_jobs = []
        scrapers = [
            self.scrape_remotive, self.scrape_linkedin, self.scrape_glassdoor,
            self.scrape_github, self.scrape_angelco, self.scrape_monster,
            self.scrape_dice, self.scrape_flexjobs, self.scrape_weworkremotely
            # Add jobserve, careerbuilder, simplyhired, ziprecruiter functions here
        ]
        for scraper in scrapers:
            jobs = scraper()
            new_count = 0
            for job in jobs:
                job_id = self.generate_job_id(job['title'], job.get('company',''), job['link'])
                if not self.is_job_seen(job_id):
                    self.mark_job_seen(job_id, job['title'], job.get('company',''), job['link'], job['portal'], job['posted_date'], job['days_ago'])
                    new_count += 1
            log(f"📊 {scraper.__name__}: {len(jobs)} jobs, New: {new_count}")
            all_jobs.extend(jobs)
        return all_jobs

# ---------------- Flask Dashboard ----------------
app = Flask(__name__)
bot = JobScraperBot()

def check_auth(username,password):
    return username == bot.dash_user and password == bot.dash_pass

def authenticate():
    return Response('Login required', 401, {'WWW-Authenticate':'Basic realm="Login"'})

@app.route("/")
def dashboard():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    jobs = bot.process_jobs()
    return render_template('index.html', jobs=jobs)

# ---------------- Main ----------------
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',8080)), debug=True)
