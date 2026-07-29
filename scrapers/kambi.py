import asyncio
import json
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class KambiScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                if 'betoffer' in url_l or 'listview' in url_l or 'offering' in url_l or 'event' in url_l:
                    data = await response.json()
                    str_data = str(data).lower()
                    
                    if 'events' in str_data and ('outcomes' in str_data or 'mainbetoffer' in str_data):
                        self.raw_data = data
            except Exception:
                pass

    async def extract_single(self, url):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)
        
        try:
            print(f"Acessando {self.bookmaker_name}: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(10000)
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
            
        self.page.remove_listener("response", self.intercept_odds)

    def _extract_offers_from_events(self, events_list):
        offers = []
        events_map = {}
        for item in events_list:
            ev = item.get("event", item)
            ev_id = ev.get("id")
            if ev_id:
                events_map[ev_id] = ev
            
            main_offer = item.get("mainBetOffer")
            if main_offer:
                main_offer["eventId"] = ev_id
                offers.append(main_offer)
                
            bet_offers = item.get("betOffers", [])
            for bo in bet_offers:
                bo["eventId"] = ev_id
                offers.append(bo)
                
        return offers, events_map

    def transform_and_load(self):
        if not self.raw_data:
            print(f"Nenhum dado interceptado na {self.bookmaker_name}.")
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)
            odds_to_insert = []
            
            events_raw = self.raw_data.get("events", [])
            bet_offers_raw = self.raw_data.get("betOffers", [])
            
            offers, events_map = self._extract_offers_from_events(events_raw)
            offers.extend(bet_offers_raw)
            
            if not offers:
                with open(f"{self.bookmaker_name.lower()}_dump.json", "w", encoding="utf-8") as f:
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)
                print("Nenhuma oferta legível encontrada. Dump gerado.")
                return

            processed_offers = set()

            for offer in offers:
                offer_id = offer.get("id")
                if offer_id:
                    if offer_id in processed_offers: continue
                    processed_offers.add(offer_id)
                
                event_id = offer.get("eventId")
                event_info = events_map.get(event_id)
                
                if not event_info:
                    for ev in events_raw:
                        ev_data = ev.get("event") if "event" in ev else ev
                        if ev_data.get("id") == event_id:
                            event_info = ev_data
                            break
                            
                if not event_info: continue

                home_team = event_info.get("homeName")
                away_team = event_info.get("awayName")
                
                if not home_team or not away_team:
                    name_str = event_info.get("name", "")
                    if not name_str: continue
                    home_team, away_team = name_str, "Adversário"
                    for sep in [" - ", " v ", " vs "]:
                        if sep in name_str:
                            home_team, away_team = name_str.split(sep, 1)
                            break
                
                start_time_str = event_info.get("start")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError:
                        pass
                
                match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Futebol", start_time)
                
                criterion = offer.get("criterion", {})
                bet_offer_type = offer.get("betOfferType", {})
                
                market_label = criterion.get("englishLabel") or criterion.get("label", "")
                type_name = bet_offer_type.get("englishName") or bet_offer_type.get("name", "")
                
                market_str = f"{market_label} {type_name}".upper()
                market_type = ""
                
                if any(m in market_str for m in ["MATCH ODDS", "FULL TIME", "1X2", "TEMPO REGULAMENTAR"]):
                    market_type = "1X2"
                elif any(m in market_str for m in ["BOTH TEAMS TO SCORE", "AMBAS"]):
                    market_type = "BTTS"
                else:
                    continue 
                
                offer_tags = offer.get("tags", [])
                is_super_odd = any(tag in offer_tags for tag in ["PRICE_BOOST", "BOOSTED", "ODDS_BOOST"])

                outcomes = offer.get("outcomes", [])
                for outcome in outcomes:
                    selection_name = outcome.get("englishLabel") or outcome.get("label", "")
                    odd_raw = outcome.get("odds", 0)
                    
                    if odd_raw > 0:
                        odd_value = odd_raw / 1000.0
                        
                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": market_type,
                            "selection": selection_name.strip(),
                            "odd_value": odd_value,
                            "is_super_odd": is_super_odd
                        })
                            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds da {self.bookmaker_name} inseridas no banco.")
            else:
                print(f"Eventos encontrados, mas sem odds no filtro de interesse (1X2/BTTS).")

        except Exception as e:
            print(f"Erro na {self.bookmaker_name}: {e}")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()