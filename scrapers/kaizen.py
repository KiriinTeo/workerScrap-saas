import asyncio
import json
import traceback
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class KaizenScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.raw_data = None

    async def extract_single(self, url):
        self.raw_data = None
        try:
            print(f"Acessando provedor Kaizen ({self.bookmaker_name}): {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(5000)
            
            ssr_data = await self.page.evaluate("() => window.initial_state || window.INITIAL_STATE")
            if ssr_data:
                self.raw_data = ssr_data
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")

    def _generate_debug_dump(self, reason="unknown"):
        if self.raw_data:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"debug_kaizen_{self.bookmaker_name.lower()}_{reason}_{timestamp}.json"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)
                print(f"[!] Dump de debug gerado: {filename} (Motivo: {reason})")
            except Exception as e:
                print(f"Falha ao salvar dump de debug: {e}")

    def _find_events(self, data, events_list):
        if isinstance(data, dict):
            if "markets" in data and "name" in data and "startTime" in data and "id" in data:
                events_list.append(data)
            for key, value in data.items():
                self._find_events(value, events_list)
        elif isinstance(data, list):
            for item in data:
                self._find_events(item, events_list)

    def transform_and_load(self):
        if not self.raw_data:
            print(f"Nenhum dado interceptado na {self.bookmaker_name}.")
            return

        db = SessionLocal()
        odds_to_insert = []
        events_raw = []
        
        self._find_events(self.raw_data, events_raw)
        
        if not events_raw:
            self._generate_debug_dump(reason="no_events")
            return

        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)

            for event in events_raw:
                event_name = event.get("name", "")
                if not event_name or (" - " not in event_name and " vs " not in event_name.lower()):
                    continue
                    
                sep = " - " if " - " in event_name else " vs "
                parts = event_name.split(sep, 1)
                home_team, away_team = parts[0].strip(), parts[1].strip()
                
                start_time = datetime.now(timezone.utc)
                start_time_raw = event.get("startTime")
                if start_time_raw:
                    try:
                        start_time = datetime.fromtimestamp(start_time_raw / 1000.0, tz=timezone.utc)
                    except Exception:
                        pass
                        
                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)
                
                markets = event.get("markets", [])
                for market in markets:
                    market_name = market.get("name", "").strip().upper()
                    
                    if not any(m in market_name for m in ["RESULTADO FINAL", "1X2", "MATCH RESULT", "VENCEDOR"]):
                        continue 
                        
                    is_super_odd = market.get("isSuperOdds", False)
                    has_early_payout = any(term in market_name for term in ["2 GOLS", "VANTAGEM", "PAGAMENTO ANTECIPADO", "EARLY PAYOUT"])
                        
                    selections = market.get("selections", [])
                    for selection in selections:
                        raw_sel_name = selection.get("name", "").strip().upper()
                        odd_value = selection.get("price", 0.0)
                        
                        if not odd_value or float(odd_value) <= 1.0:
                            continue
                            
                        sel_code = None
                        if raw_sel_name in ["1", home_team.upper()]:
                            sel_code = "1"
                        elif raw_sel_name in ["X", "EMPATE", "DRAW"]:
                            sel_code = "X"
                        elif raw_sel_name in ["2", away_team.upper()]:
                            sel_code = "2"
                            
                        if not sel_code:
                            continue

                        is_vitoria = sel_code in ["1", "2"]
                        is_empate = sel_code == "X"

                        if is_vitoria and not has_early_payout:
                            continue 
                        
                        if is_empate and not is_super_odd:
                            continue 

                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": "1X2",
                            "selection": sel_code,
                            "odd_value": float(odd_value),
                            "is_super_odd": bool(is_super_odd)
                        })
                        
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds filtradas do provedor Kaizen ({self.bookmaker_name}) inseridas no banco.")
                self._generate_debug_dump(reason="success")
            else:
                print(f"Eventos processados, mas nenhuma odd passou no filtro restrito (Vitória+EP ou Empate+SuperOdd).")
                self._generate_debug_dump(reason="no_valid_odds")

        except Exception as e:
            print(f"Erro no processamento da {self.bookmaker_name}: {e}")
            traceback.print_exc()
            self._generate_debug_dump(reason="exception_raised")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()