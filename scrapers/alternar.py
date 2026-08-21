import json
import re
import traceback
from .base_scraper import BaseScraper

class AltenarScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('vaidebet', headless)
        self.raw_payloads = []
        self.target_champ_id = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                if 'sportsbook' in url_l or 'events' in url_l or 'api' in url_l:
                    data = await response.json()
                    if isinstance(data, dict) and ('events' in data or 'markets' in data):
                        self.raw_payloads.append(data)
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_payloads = []
        self.target_champ_id = None
        
        match = re.search(r'championship/(\d+)', url)
        if match:
            self.target_champ_id = int(match.group(1))

        self.page.on("response", self.intercept_odds)

        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.evaluate("window.scrollBy(0, 800)")
        await self.page.wait_for_timeout(8000)
        
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_payloads:
            import os
            os.makedirs("dumps", exist_ok=True)
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_payloads, f, indent=2, ensure_ascii=False)
            print(f"Dump da {self.house_name.upper()} salvo com {len(self.raw_payloads)} payloads.")

        if self.raw_payloads:
            await self._parse_altenar_data(self.raw_payloads)
        else:
            print("[ERRO ALTENAR] Nenhum JSON capturado.")

    async def _parse_altenar_data(self, payloads):
        try:
            print(f"[DEBUG PARSER] Iniciando analise em {len(payloads)} payloads Altenar...")
            
            processed_ids = set()

            for data in payloads:
                events_raw = data.get("events", [])
                markets_raw = data.get("markets", [])
                odds_raw = data.get("odds", [])
                champs_raw = data.get("champs", [])
                
                champ_ep = {}
                if isinstance(champs_raw, list):
                    for c in champs_raw:
                        if isinstance(c, dict):
                            c_id = c.get("id")
                            has_ep = any(isinstance(o, dict) and o.get("type") == 0 for o in c.get("offers", []))
                            champ_ep[c_id] = has_ep
                
                markets_dict = {m.get("id"): m for m in markets_raw if isinstance(m, dict)}
                odds_dict = {o.get("id"): o for o in odds_raw if isinstance(o, dict)}

                for event in events_raw:
                    if not isinstance(event, dict):
                        continue
                    
                    champ_id = event.get("champId")
                    
                    if self.target_champ_id and champ_id != self.target_champ_id:
                        continue
                        
                    match_id = event.get("id")
                    event_name = event.get("name")
                    
                    if not match_id or not event_name:
                        continue

                    if match_id in processed_ids:
                        continue
                    processed_ids.add(match_id)

                    teams = str(event_name).split(' vs. ')
                    if len(teams) != 2:
                        teams = str(event_name).split(' - ')
                    if len(teams) != 2:
                        if ' vs ' in str(event_name).lower():
                            teams = str(event_name).lower().split(' vs ')
                        else:
                            continue
                    
                    home_team, away_team = teams[0].strip(), teams[1].strip()
                    
                    market_ids = event.get("marketIds", [])
                    if not isinstance(market_ids, list):
                        continue

                    has_early_payout_event = champ_ep.get(champ_id, False)
                    
                    event_offers = event.get("offers", [])
                    if isinstance(event_offers, list):
                        if any(isinstance(o, dict) and o.get("type") == 0 for o in event_offers):
                            has_early_payout_event = True

                    print(f"[JOGO ENCONTRADO] {home_team} x {away_team} | Mercados: {len(market_ids)}")

                    for m_id in market_ids:
                        market = markets_dict.get(m_id)
                        if not market:
                            continue

                        market_name = market.get("name", "").lower()
                        market_type = market.get("typeId")
                        
                        is_super_odd_market = "superodd" in market_name or "super odd" in market_name
                        
                        if market_type != 1 and not is_super_odd_market and not any(m in market_name for m in ["vencedor", "1x2", "resultado"]):
                            continue
                            
                        print(f"   [MERCADO ALVO] {market_name} (Type: {market_type})")
                            
                        has_ep_market = any(term in market_name for term in ["pagamento antecipado", "early payout", "vantagem", "2 up", "2 gols", "vp", "(vp)", "+2"])
                        
                        odd_ids = market.get("oddIds", [])
                        if not isinstance(odd_ids, list):
                            continue

                        for o_id in odd_ids:
                            odd_data = odds_dict.get(o_id)
                            if not odd_data:
                                continue
                            
                            odd_value = odd_data.get("price", 0.0)
                            raw_sel_name = odd_data.get("name", "")
                            
                            if not raw_sel_name or not odd_value:
                                continue

                            is_super_odd = odd_data.get("isDBB") == True or is_super_odd_market
                            
                            sel_clean = str(raw_sel_name).lower()
                            is_vitoria = sel_clean in ['1', '2'] or sel_clean in home_team.lower() or sel_clean in away_team.lower()
                            
                            has_early_payout = False
                            if is_vitoria and (has_early_payout_event or has_ep_market or market_type == 1):
                                has_early_payout = True

                            await self.process_and_store_odd(
                                match_id=match_id,
                                home_team=home_team,
                                away_team=away_team,
                                selection_name=raw_sel_name,
                                odd_value=odd_value,
                                has_early_payout=has_early_payout,
                                is_super_odd=is_super_odd
                            )
        except Exception as e:
            print(f"[ERRO PARSER] {e}")
            traceback.print_exc()