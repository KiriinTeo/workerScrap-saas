import json
from .base_scraper import BaseScraper

class KaizenScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('betano', headless)
        self.raw_payloads = []

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                ignorar = ['offer', 'banner', 'config', 'tracker', 'user', 'virtual']
                
                if 'api' in url_l and not any(x in url_l for x in ignorar):
                    data = await response.json()
                    str_data = str(data)
                    if 'markets' in str_data and ('shortName' in str_data or 'name' in str_data):
                        if 'liveOverviewMarketList' not in str_data:
                            self.raw_payloads.append(data)
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_payloads = []
        self.page.on("response", self.intercept_odds)

        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.evaluate("window.scrollBy(0, 800)")
        await self.page.wait_for_timeout(6000)

        ssr_data = await self.page.evaluate('''() => {
            let state = window.initial_state || window.__INITIAL_STATE__;
            return state ? JSON.stringify(state) : null;
        }''')

        if ssr_data:
            try:
                state_json = json.loads(ssr_data)
                self.raw_payloads.append(state_json)
                print("[KAIZEN] Estado global (SSR) capturado com sucesso do HTML.")
            except Exception:
                pass
        
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_payloads:
            import os
            os.makedirs("dumps", exist_ok=True)
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_payloads, f, indent=2, ensure_ascii=False)
            print(f"Dump da {self.house_name.upper()} salvo com {len(self.raw_payloads)} payloads validados!")

        if self.raw_payloads:
            await self._parse_kaizen_data(self.raw_payloads)
        else:
            print("[ERRO KAIZEN] Nem SSR e nem Interceptor capturaram os jogos válidos.")

    async def _parse_kaizen_data(self, payloads):
        try:
            print(f"[DEBUG PARSER] Iniciando análise em {len(payloads)} payloads de alta qualidade...")

            all_events = []
            for data in payloads:
                if isinstance(data, dict) and 'data' in data and isinstance(data['data'], dict) and 'events' in data['data']:
                    all_events.extend(data['data']['events'])
                else:
                    all_events.extend(self._find_events_recursively(data))

            processed_ids = set()
            eventos_validos = 0

            for event in all_events:
                if not isinstance(event, dict):
                    continue

                match_id = event.get('id') or event.get('betRadarId') or event.get('eventId')
                event_name = event.get('name') or event.get('shortName')

                if not event_name and 'participants' in event and isinstance(event['participants'], list):
                    parts = event['participants']
                    if len(parts) >= 2 and isinstance(parts[0], dict) and isinstance(parts[1], dict):
                        event_name = f"{parts[0].get('name', 'Casa')} - {parts[1].get('name', 'Fora')}"

                if not match_id or not event_name:
                    continue

                if match_id in processed_ids:
                    continue
                processed_ids.add(match_id)

                teams = str(event_name).split(' - ')
                if len(teams) != 2:
                    if ' vs ' in str(event_name).lower():
                        teams = str(event_name).lower().split(' vs ')
                    else:
                        continue
                
                home_team, away_team = teams[0].strip(), teams[1].strip()
                markets = event.get('markets', [])
                
                if not isinstance(markets, list) or len(markets) == 0:
                    continue

                eventos_validos += 1
                print(f"[JOGO ENCONTRADO] {home_team} vs {away_team} | Mercados Disponíveis: {len(markets)}")

                for market in markets:
                    if not isinstance(market, dict):
                        continue

                    market_type = market.get('type', '')
                    market_name = market.get('name', '').lower()

                    is_super_odd_market = "superodd" in market_name or market.get('hasSuperOdds') == True

                    if market_type not in ['MRES', 'MR'] and not is_super_odd_market and 'resultado' not in market_name:
                        continue
                    
                    selections = market.get('selections', [])
                    if not isinstance(selections, list):
                        continue

                    for sel in selections:
                        if not isinstance(sel, dict):
                            continue

                        sel_name = sel.get('name')
                        odd_value = sel.get('price')
                        tags = sel.get('tags', [])
                        
                        if not isinstance(tags, list):
                            tags = []

                        if not sel_name or odd_value is None:
                            continue

                        is_vitoria = sel_name.upper() in ['1', '2'] or sel_name.lower() in home_team.lower() or sel_name.lower() in away_team.lower()
                        
                        has_early_payout = False
                        if is_vitoria and market_type in ['MRES', 'MR']:
                            has_early_payout = True 
                        elif sel.get('isEarlyPayout') == True or "EP" in tags:
                            has_early_payout = True

                        is_super_odd = is_super_odd_market or sel.get('isSuperOdds') == True or "SO" in tags

                        await self.process_and_store_odd(
                            match_id=match_id,
                            home_team=home_team,
                            away_team=away_team,
                            selection_name=sel_name,
                            odd_value=odd_value,
                            has_early_payout=has_early_payout,
                            is_super_odd=is_super_odd
                        )

            print(f"Análise concluída. {eventos_validos} jogos mapeados e validados.")

        except Exception as e:
            import traceback
            print(f"[ERRO PARSER] Erro crítico: {e}")
            traceback.print_exc()

    def _find_events_recursively(self, obj):
        found = []
        if isinstance(obj, dict):
            if 'events' in obj and isinstance(obj['events'], list):
                found.extend(obj['events'])
            
            if 'markets' in obj and ('shortName' in obj or 'name' in obj):
                found.append(obj)

            for k, v in obj.items():
                if k != 'events':
                    found.extend(self._find_events_recursively(v))
                
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._find_events_recursively(item))
        return found