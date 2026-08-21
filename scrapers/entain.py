import json
import re
import traceback
from .base_scraper import BaseScraper

class EntainScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('sportingbet', headless)
        self.raw_payloads = []
        self.target_comp_id = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                ignorar = ['config', 'banner', 'virtual', 'user', 'icon', 'analytics', 'tracking']
                
                if not any(x in url_l for x in ignorar):
                    data = await response.json()
                    str_data = str(data)
                    if 'fixtures' in str_data and 'games' in str_data:
                        self.raw_payloads.append(data)
                        print(f" [REDE] Payload valido capturado da URL: {url_l[:80]}...")
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_payloads = []
        self.target_comp_id = None
        
        match = re.search(r'-(\d+)/?$', url)
        if match:
            self.target_comp_id = str(match.group(1))

        self.page.on("response", self.intercept_odds)

        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.evaluate("window.scrollBy(0, 1000)")
        await self.page.wait_for_timeout(8000)

        ssr_data = await self.page.evaluate('''() => {
            let state = window.initial_state || window.__INITIAL_STATE__ || window.state || window.env;
            return state ? JSON.stringify(state) : null;
        }''')

        if ssr_data:
            try:
                state_json = json.loads(ssr_data)
                if 'fixtures' in str(state_json):
                    self.raw_payloads.append(state_json)
                    print(" [ENTAIN] Estado global (SSR) capturado com sucesso.")
            except Exception:
                pass
        
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_payloads:
            import os
            os.makedirs("dumps", exist_ok=True)
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_payloads, f, indent=2, ensure_ascii=False)
            print(f"Dump da {self.house_name.upper()} salvo com {len(self.raw_payloads)} payloads.")

        if self.raw_payloads:
            await self._parse_entain_data(self.raw_payloads)
        else:
            print(" [ERRO ENTAIN] Nenhum JSON capturado.")

    async def _parse_entain_data(self, payloads):
        try:
            print(f"[DEBUG PARSER] Analisando {len(payloads)} payloads da Entain...")
            
            all_fixtures = []
            for data in payloads:
                all_fixtures.extend(self._find_fixtures_recursively(data))

            print(f"[DEBUG PARSER] Encontrados {len(all_fixtures)} nos de fixtures.")

            processed_ids = set()

            for fixture in all_fixtures:
                if not isinstance(fixture, dict):
                    continue
                
                comp_info = fixture.get("competition", {})
                if isinstance(comp_info, dict):
                    comp_id = comp_info.get("id")
                    if self.target_comp_id and comp_id and str(comp_id) != self.target_comp_id:
                        continue

                match_id = fixture.get("id")
                name_obj = fixture.get("name", {})
                if isinstance(name_obj, dict):
                    event_name = name_obj.get("value")
                else:
                    event_name = str(name_obj)

                if not match_id or not event_name or event_name == "{}":
                    continue

                if match_id in processed_ids:
                    continue
                processed_ids.add(match_id)

                teams = str(event_name).split(' - ')
                if len(teams) != 2:
                    teams = str(event_name).split(' vs ')
                if len(teams) != 2:
                    if ' v ' in str(event_name).lower():
                        teams = str(event_name).lower().split(' v ')
                    else:
                        continue

                home_team, away_team = teams[0].strip(), teams[1].strip()

                games = fixture.get("games", [])
                if not isinstance(games, list):
                    games = []
                
                option_markets = fixture.get("optionMarkets", [])
                if isinstance(option_markets, list):
                    games.extend(option_markets)

                if not games:
                    continue

                print(f"[JOGO ENCONTRADO] {home_team} x {away_team} | Mercados: {len(games)}")

                for game in games:
                    if not isinstance(game, dict):
                        continue
                    
                    market_name_obj = game.get("name", {})
                    if isinstance(market_name_obj, dict):
                        market_name = str(market_name_obj.get("value", "")).lower()
                    else:
                        market_name = str(market_name_obj).lower()

                    is_main_market = any(m in market_name for m in ["resultado da partida", "vencedor do encontro", "1x2", "resultado do jogo", "tempo regulamentar", "match result"])
                    is_super_odd_market = "super odd" in market_name or "cotas aumentadas" in market_name or "price boost" in market_name

                    if not is_main_market and not is_super_odd_market:
                        continue
                    
                    print(f"   [MERCADO ALVO] {market_name}")

                    is_brasileirao = self.target_comp_id == "102838"
                    has_ep_market = any(term in market_name for term in ["pagamento antecipado", "vantagem premiada", "vp", "early payout", "2 gols", "2 up"])
                    
                    if is_brasileirao and is_main_market:
                        has_ep_market = True

                    options_list = game.get("options", [])
                    if not isinstance(options_list, list):
                        options_list = game.get("results", [])
                        
                    if not isinstance(options_list, list):
                        continue

                    for option in options_list:
                        if not isinstance(option, dict):
                            continue

                        sel_name_obj = option.get("name", {})
                        if isinstance(sel_name_obj, dict):
                            sel_name = str(sel_name_obj.get("value", ""))
                        else:
                            sel_name = str(sel_name_obj)

                        price_obj = option.get("price", {})
                        odd_value = 0.0

                        if isinstance(price_obj, dict):
                            odd_value = float(price_obj.get("odds", 0.0))
                            if odd_value <= 1.0:
                                num = price_obj.get("numerator")
                                den = price_obj.get("denominator")
                                if num is not None and den is not None and float(den) != 0:
                                    odd_value = (float(num) / float(den)) + 1.0
                        elif isinstance(option.get("odds"), (int, float)):
                            odd_value = float(option.get("odds"))

                        if not sel_name or odd_value <= 1.0:
                            continue

                        is_super_odd = option.get("isBoosted") == True or is_super_odd_market

                        sel_clean = sel_name.lower()
                        is_vitoria = sel_clean in ['1', '2'] or sel_clean in home_team.lower() or sel_clean in away_team.lower()
                        is_empate = sel_clean in ['x', 'empate', 'draw']
                        
                        has_early_payout = False
                        if is_vitoria and has_ep_market:
                            has_early_payout = True

                        print(f"   [RAIO-X] Sel: '{sel_name}' @ {round(odd_value, 2)} | EP: {has_early_payout} | SO: {is_super_odd}")

                        if (is_vitoria and has_early_payout) or (is_empate and is_super_odd):
                            print(f"      APROVADO!")
                            await self.process_and_store_odd(
                                match_id=match_id,
                                home_team=home_team,
                                away_team=away_team,
                                selection_name=sel_name,
                                odd_value=round(odd_value, 2),
                                has_early_payout=has_early_payout,
                                is_super_odd=is_super_odd
                            )
                        else:
                            print(f"      REPROVADO.")

        except Exception:
            traceback.print_exc()

    def _find_fixtures_recursively(self, obj):
        found = []
        if isinstance(obj, dict):
            if 'fixtures' in obj and isinstance(obj['fixtures'], list):
                found.extend(obj['fixtures'])
            
            if 'games' in obj and 'name' in obj and isinstance(obj['name'], dict) and 'value' in obj['name']:
                found.append(obj)

            for k, v in obj.items():
                if k != 'fixtures':
                    found.extend(self._find_fixtures_recursively(v))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._find_fixtures_recursively(item))
        return found