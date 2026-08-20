import json
from .base_scraper import BaseScraper

class KaizenScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('betano', headless)

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                if 'api' in response.url:
                    data = await response.json()
                    if 'data' in data and ('blocks' in data['data'] or 'events' in str(data)):
                        self.raw_data = data
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)

        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.wait_for_timeout(8000) 
        
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_data:
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_data, f, indent=2, ensure_ascii=False)
            print(f"Dump da {self.house_name.upper()} gerado com sucesso!")
        elif save_dump:
            print(f"Falha: Nenhum dado capturado no interceptor da {self.house_name.upper()}.")

        await self._parse_kaizen_data()

    async def _parse_kaizen_data(self, data):
        try:
            events = data.get('data', {}).get('events', []) 
            
            if not events:
                events = self._find_events_recursively(data)

            for event in events:
                match_id = event.get('id')
                
                if not match_id or 'name' not in event:
                    continue

                teams = event['name'].split(' - ')
                if len(teams) != 2:
                    continue
                
                home_team, away_team = teams[0].strip(), teams[1].strip()
                markets = event.get('markets', [])

                for market in markets:
                    market_name = market.get('name', '').lower()
                    if 'resultado final' not in market_name and market.get('type') != 'MR':
                        continue

                    selections = market.get('selections', [])
                    for sel in selections:
                        sel_name = sel.get('name')
                        odd_value = sel.get('price')

                        tags = sel.get('tags', [])
                        
                        has_early_payout = False
                        is_super_odd = False

                        if sel.get('isEarlyPayout') == True or "EP" in tags or "2GoalsAhead" in tags:
                            has_early_payout = True
                            
                        if sel.get('isSuperOdds') == True or "SO" in tags or market.get('hasSuperOdds') == True:
                            is_super_odd = True

                        await self.process_and_store_odd(
                            match_id=match_id,
                            home_team=home_team,
                            away_team=away_team,
                            selection_name=sel_name,
                            odd_value=odd_value,
                            has_early_payout=has_early_payout,
                            is_super_odd=is_super_odd
                        )

        except Exception as e:
            print(f"Erro ao parsear dados da Kaizen: {e}")

    def _find_events_recursively(self, obj):
        found = []
        if isinstance(obj, dict):
            if 'markets' in obj and 'name' in obj and 'id' in obj and 'startTime' in obj:
                found.append(obj)
            else:
                for v in obj.values():
                    found.extend(self._find_events_recursively(v))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._find_events_recursively(item))
        return found