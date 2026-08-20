import json
import asyncio
import redis.asyncio as redis
from datetime import datetime, timezone
from playwright.async_api import async_playwright

class BaseScraper:
    def __init__(self, house_name, headless=True):
        self.house_name = house_name.lower() # ex: 'betano', 'bet365'
        self.headless = headless
        
        # recursos do playwright
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        # conexão assíncrona com o Redis
        self.redis = redis.from_url("redis://localhost:6379/0", decode_responses=True)
        
        # TTL: tempo de vida da Odd no Redis (15 minutos = 900 segundos)
        self.ttl_seconds = 900 

    async def init_browser(self):
        """inicializa o navegador blindado contra detecção básica de bots."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars"
            ])
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()

    async def process_and_store_odd(self, match_id, home_team, away_team, selection_name, odd_value, has_early_payout, is_super_odd):
        is_vitoria = selection_name.lower() in [home_team.lower(), away_team.lower(), '1', '2', 'home', 'away']
        is_empate = selection_name.lower() in ['empate', 'draw', 'x']

        is_valid_opportunity = False
        if is_vitoria and has_early_payout:
            is_valid_opportunity = True
        elif is_empate and is_super_odd:
            is_valid_opportunity = True

        if not is_valid_opportunity:
            return False

        payload = {
            "house": self.house_name,
            "match_id": str(match_id),
            "home_team": home_team,
            "away_team": away_team,
            "selection": selection_name,
            "odd": float(odd_value),
            "has_early_payout": bool(has_early_payout),
            "is_super_odd": bool(is_super_odd),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        redis_key = f"raw_odd:{self.house_name}:{match_id}"
        
        await self.redis.setex(name=redis_key, time=self.ttl_seconds, value=json.dumps(payload))
        print(f"[{self.house_name.upper()}] Odd capturada e salva: {home_team} vs {away_team} | Sel: {selection_name} @ {odd_value}")
        return True

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if self.redis:
            await self.redis.aclose()