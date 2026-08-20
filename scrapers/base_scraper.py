import json
import asyncio
import redis.asyncio as redis
from datetime import datetime, timezone
from playwright.async_api import async_playwright

class BaseScraper:
    def __init__(self, house_name, headless=True):
        self.house_name = house_name.lower()
        self.headless = headless
        
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        self.redis = redis.from_url("redis://localhost:6379/0", decode_responses=True, protocol="2")
        self.ttl_seconds = 900 

    async def init_browser(self):
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
        print(f"[RAIO-X] Recebido: {home_team} x {away_team} | Sel: '{selection_name}' @ {odd_value} | EP: {has_early_payout} | SO: {is_super_odd}")

        # Limpeza pesada de strings para evitar erros de espaços em branco ou maiúsculas
        home_clean = home_team.lower().strip()
        away_clean = away_team.lower().strip()
        sel_clean = str(selection_name).lower().strip()

        is_vitoria = (
            sel_clean in ['1', '2', 'home', 'away'] or 
            sel_clean in home_clean or 
            home_clean in sel_clean or 
            sel_clean in away_clean or 
            away_clean in sel_clean
        )
        
        is_empate = sel_clean in ['empate', 'draw', 'x']

        is_valid_opportunity = False
        if is_vitoria and has_early_payout:
            is_valid_opportunity = True
            print(f"    APROVADO: É Vitória e tem Pagamento Antecipado!")
        elif is_empate and is_super_odd:
            is_valid_opportunity = True
            print(f"    APROVADO: É Empate e tem Super Odd!")
        else:
            print(f"    REPROVADO: Não atende à regra de ouro (is_vitoria={is_vitoria}, is_empate={is_empate}).")

        if not is_valid_opportunity:
            # Não salva no Redis e interrompe
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
        
        try:
            await self.redis.setex(name=redis_key, time=self.ttl_seconds, value=json.dumps(payload))
            print(f"    SALVO NO REDIS COM SUCESSO: Chave {redis_key}")
        except Exception as e:
            print(f"    [ERRO REDIS] A odd foi aprovada, mas o banco falhou: {e}")
            
        return True

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        if self.redis:
            await self.redis.aclose()