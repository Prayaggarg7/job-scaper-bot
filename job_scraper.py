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
import json
import xml.etree.ElementTree as ET

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
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '300'))  # default 5 min
        self.max_days_old = int(os.getenv('MAX_DAYS_OLD', '10'))
        self.dash_user = os.getenv('DASHBOARD_USER', 'admin')
        self.dash_pass = os.getenv('DASHBOARD_PASS', 'password')

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

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

    def scrape_remoteok(self):
        jobs=[]
        url="https://remoteok.com/api"
        log("🔍 Scraping RemoteOK jobs...")
        try:
            r=requests.get(url,headers=self.headers,timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code==200:
                data=r.json()
                for job in data[1:31]:
                    if not isinstance(job,dict): continue
                    epoch=job.get('epoch',0)
                    job_date=datetime.fromtimestamp(epoch) if epoch else datetime.now()
                    days_ago=(datetime.now()-job_date).days
                    if not self.is_recent_job(days_ago): continue
                    job_text=f"{job.get('position','')} {job.get('description','')} {' '.join(job.get('tags',[]))}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': job.get('position','N/A'),
                            'company': job.get('company','N/A'),
                            'link': job.get('url',''),
                            'portal': 'RemoteOK',
                            'posted_date': job.get('date','N/A'),
                            'days_ago': days_ago
                        })
            log(f"✅ Found {len(jobs)} jobs on RemoteOK")
        except Exception as e:
            log(f"❌ RemoteOK scrape error: {e}")
        return jobs

    def scrape_linkedin(self):
        jobs = []
        query = quote_plus(' OR '.join(self.skills[:3]))
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={query}&location=Worldwide&f_TPR=r86400&start=0"
        log("🔍 Scraping LinkedIn jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
                            days_ago = 1  # LinkedIn RSS doesn't provide exact dates easily
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on LinkedIn")
        except Exception as e:
            log(f"❌ LinkedIn scrape error: {e}")
        return jobs

    def scrape_glassdoor(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:2]))
        url = f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}&fromAge=10"
        log("🔍 Scraping Glassdoor jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
                            days_ago = 3  # Default approximation
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on Glassdoor")
        except Exception as e:
            log(f"❌ Glassdoor scrape error: {e}")
        return jobs

    def scrape_stackoverflow(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://stackoverflow.com/jobs/feed?q={query}&r=true"
        log("🔍 Scraping Stack Overflow jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'xml')
                items = soup.find_all('item')[:20]
                for item in items:
                    title = item.find('title').text if item.find('title') else 'N/A'
                    link = item.find('link').text if item.find('link') else ''
                    desc = item.find('description').text if item.find('description') else ''
                    pub_date = item.find('pubDate').text if item.find('pubDate') else ''
                    company = item.find('company').text if item.find('company') else 'N/A'
                    
                    try:
                        job_date = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
                        days_ago = (datetime.now() - job_date).days
                    except:
                        days_ago = 0
                    
                    if not self.is_recent_job(days_ago):
                        continue
                    
                    job_text = f"{title} {desc} {company}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': title,
                            'company': company,
                            'link': link,
                            'portal': 'Stack Overflow',
                            'posted_date': pub_date,
                            'days_ago': days_ago
                        })
            log(f"✅ Found {len(jobs)} jobs on Stack Overflow")
        except Exception as e:
            log(f"❌ Stack Overflow scrape error: {e}")
        return jobs

    def scrape_github(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://jobs.github.com/positions.json?description={query}&full_time=true"
        log("🔍 Scraping GitHub Jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
            log(f"✅ Found {len(jobs)} jobs on GitHub Jobs")
        except Exception as e:
            log(f"❌ GitHub Jobs scrape error: {e}")
        return jobs

    def scrape_angelco(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://angel.co/jobs?filter={query}"
        log("🔍 Scraping AngelList jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
                            days_ago = 2  # Default approximation
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on AngelList")
        except Exception as e:
            log(f"❌ AngelList scrape error: {e}")
        return jobs

    def scrape_monster(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.monster.com/jobs/search/?q={query}&where=remote&fromage=10"
        log("🔍 Scraping Monster jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('section', class_='card-content')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('h2', class_='title')
                        company_elem = card.find('div', class_='company')
                        location_elem = card.find('div', class_='location')
                        link_elem = card.find('a')
                        
                        if title_elem and link_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = link_elem.get('href', '')
                            days_ago = 4  # Default approximation
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on Monster")
        except Exception as e:
            log(f"❌ Monster scrape error: {e}")
        return jobs

    def scrape_dice(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.dice.com/jobs?q={query}&countryCode=US&radius=30&radiusUnit=mi&page=1&pageSize=20&filters.remote=true&language=en"
        log("🔍 Scraping Dice jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
                            days_ago = 3  # Default approximation
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on Dice")
        except Exception as e:
            log(f"❌ Dice scrape error: {e}")
        return jobs

    def scrape_flexjobs(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.flexjobs.com/search?search={query}&location=&srsltid=AfmBOooYZWf0J_XnTd9p0FQcFcJ6z4hLcQv7J5xwYv6pKZvVtYq9XzYg"
        log("🔍 Scraping FlexJobs jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
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
                            days_ago = 2  # Default approximation
                            
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
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on FlexJobs")
        except Exception as e:
            log(f"❌ FlexJobs scrape error: {e}")
        return jobs

    def scrape_weworkremotely(self):
        jobs = []
        url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        log("🔍 Scraping We Work Remotely jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'xml')
                items = soup.find_all('item')[:20]
                for item in items:
                    title = item.find('title').text if item.find('title') else 'N/A'
                    link = item.find('link').text if item.find('link') else ''
                    desc = item.find('description').text if item.find('description') else ''
                    pub_date = item.find('pubDate').text if item.find('pubDate') else ''
                    
                    try:
                        job_date = datetime.strptime(pub_date, '%a, %d %b %Y %H:%M:%S %Z')
                        days_ago = (datetime.now() - job_date).days
                    except:
                        days_ago = 0
                    
                    if not self.is_recent_job(days_ago):
                        continue
                    
                    # Extract company from description or title
                    company = 'N/A'
                    if ' - ' in title:
                        company = title.split(' - ')[0]
                    
                    job_text = f"{title} {desc}"
                    if self.matches_skills(job_text):
                        jobs.append({
                            'title': title,
                            'company': company,
                            'link': link,
                            'portal': 'We Work Remotely',
                            'posted_date': pub_date,
                            'days_ago': days_ago
                        })
            log(f"✅ Found {len(jobs)} jobs on We Work Remotely")
        except Exception as e:
            log(f"❌ We Work Remotely scrape error: {e}")
        return jobs

    def scrape_jobserve(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.jobserve.com/gb/en/JobSearch.aspx?shid={hashlib.md5(query.encode()).hexdigest()[:8]}"
        log("🔍 Scraping Jobserve jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_rows = soup.find_all('tr', class_='jobsum')[:15]
                for row in job_rows:
                    try:
                        title_elem = row.find('a', class_='jobtitle')
                        company_elem = row.find('span', class_='companyname')
                        
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://www.jobserve.com" + title_elem.get('href', '') if title_elem.get('href') else ''
                            days_ago = 3  # Default approximation
                            
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'Jobserve',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on Jobserve")
        except Exception as e:
            log(f"❌ Jobserve scrape error: {e}")
        return jobs

    def scrape_careerbuilder(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.careerbuilder.com/jobs?keywords={query}&location=remote"
        log("🔍 Scraping CareerBuilder jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('div', class_='data-results-content-parent')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('div', class_='data-results-title')
                        company_elem = card.find('div', class_='data-details')
                        link_elem = card.find('a')
                        
                        if title_elem and link_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://www.careerbuilder.com" + link_elem.get('href', '') if link_elem.get('href') else ''
                            days_ago = 4  # Default approximation
                            
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'CareerBuilder',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on CareerBuilder")
        except Exception as e:
            log(f"❌ CareerBuilder scrape error: {e}")
        return jobs

    def scrape_simplyhired(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.simplyhired.com/search?q={query}&l=remote"
        log("🔍 Scraping SimplyHired jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('div', class_='SerpJob-jobCard')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('a', class_='SerpJob-link')
                        company_elem = card.find('span', class_='JobPosting-labelWithIcon')
                        
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = "https://www.simplyhired.com" + title_elem.get('href', '') if title_elem.get('href') else ''
                            days_ago = 2  # Default approximation
                            
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'SimplyHired',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on SimplyHired")
        except Exception as e:
            log(f"❌ SimplyHired scrape error: {e}")
        return jobs

    def scrape_ziprecruiter(self):
        jobs = []
        query = quote_plus(' '.join(self.skills[:3]))
        url = f"https://www.ziprecruiter.com/candidate/search?search={query}&location=remote"
        log("🔍 Scraping ZipRecruiter jobs...")
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            log(f"HTTP Status: {r.status_code}")
            if r.status_code == 200:
                soup = BeautifulSoup(r.content, 'html.parser')
                job_cards = soup.find_all('div', class_='job_content')[:15]
                for card in job_cards:
                    try:
                        title_elem = card.find('a', class_='job_link')
                        company_elem = card.find('a', class_='company_name')
                        
                        if title_elem:
                            title = title_elem.text.strip()
                            company = company_elem.text.strip() if company_elem else 'N/A'
                            link = title_elem.get('href', '')
                            days_ago = 3  # Default approximation
                            
                            job_text = f"{title} {company}"
                            if self.matches_skills(job_text):
                                jobs.append({
                                    'title': title,
                                    'company': company,
                                    'link': link,
                                    'portal': 'ZipRecruiter',
                                    'posted_date': 'Recently',
                                    'days_ago': days_ago
                                })
                    except Exception as e:
                        continue
            log(f"✅ Found {len(jobs)} jobs on ZipRecruiter")
        except Exception as e:
            log(f"❌ ZipRecruiter scrape error: {e}")
        return jobs

    # ---------- Aggregate ----------
    def scrape_all_portals(self):
        all_jobs = []
        scrapers = [
            self.scrape_remotive,
            self.scrape_indeed,
            self.scrape_remoteok,
            self.scrape_linkedin,
            self.scrape_glassdoor,
            self.scrape_stackoverflow,
            self.scrape_github,
            self.scrape_angelco,
            self.scrape_monster,
            self.scrape_dice,
            self.scrape_flexjobs,
            self.scrape_weworkremotely,
            self.scrape_jobserve,
            self.scrape_careerbuilder,
            self.scrape_simplyhired,
            self.scrape_ziprecruiter
        ]
        
        for scraper in scrapers:
            try:
                log(f"🔄 Starting {scraper.__name__}...")
                jobs = scraper()
                all_jobs.extend(jobs)
                log(f"✅ {scraper.__name__} completed with {len(jobs)} jobs")
            except Exception as e:
                log(f"❌ Scraper {scraper.__name__} exception: {e}")
            time.sleep(2)  # Be respectful to the servers
        
        return all_jobs

    def process_jobs(self):
        jobs = self.scrape_all_portals()
        jobs.sort(key=lambda x: x['days_ago'])
        new_count = 0
        for job in jobs:
            job_id = self.generate_job_id(job['title'], job.get('company',''), job['link'])
            if not self.is_job_seen(job_id):
                if self.telegram_bot_token and self.telegram_chat_id:
                    self.send_telegram(job)
                self.mark_job_seen(job_id, job['title'], job.get('company',''), job['link'], job['portal'], job['posted_date'], job['days_ago'])
                new_count += 1
        log(f"📊 Total jobs: {len(jobs)}, New: {new_count}")
        return jobs

    def send_telegram(self, job):
        msg = f"New Job: {job['title']} at {job.get('company','N/A')} [{job['portal']}] {job['link']}"
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {'chat_id': self.telegram_chat_id, 'text': msg}
        try:
            r = requests.post(url, json=payload, timeout=10)
            log(f"Telegram response: {r.status_code}")
        except Exception as e:
            log(f"❌ Telegram error: {e}")

# ---------------- Flask Dashboard ----------------
app = Flask(__name__)
bot = JobScraperBot()

# Simple auth
def check_auth(username, password):
    return username == bot.dash_user and password == bot.dash_pass

def authenticate():
    return Response('Login required', 401, {'WWW-Authenticate': 'Basic realm="Login"'})

@app.route("/")
def index():
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    jobs = bot.process_jobs()
    return render_template('index.html', jobs=jobs)

if __name__ == "__main__":
    log("🤖 Job Scraper & Dashboard starting...")
    app.run(host="0.0.0.0", port=int(os.getenv('PORT', 8080)))