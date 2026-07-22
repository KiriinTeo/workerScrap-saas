import asyncio
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal
from datetime import datetime, timezone

class BetMGMScraper(BaseScraper):
    def __init__(self, target_url, headless=True):
        super().__init__(headless)
        self.target_url = target_url
        self.raw_data = None

    async def intercept_odds(self, response):
        if "api/v1/display" in response.url and response.status == 200:
            try:
                self.raw_data = await response.json()
            except Exception:
                pass

    async def extract(self):
        await self.init_browser()
        self.page.on("response", self.intercept_odds)
        
        await self.page.goto(self.target_url, wait_until="networkidle")
        await self.page.wait_for_timeout(5000)
        
        await self.close_browser()

    def transform_and_load(self):
        if not self.raw_data:
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, "BetMGM", "https://sports.betmgm.com")
            
            odds_to_insert = []
            
            bulk_insert_odds(db, odds_to_insert)

        except Exception as e:
            raise e
        finally:
            db.close()

    async def run(self):
        await self.extract()
        self.transform_and_load()