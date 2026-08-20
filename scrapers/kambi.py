import json
from .base_scraper import BaseScraper

class KambiScraper(BaseScraper):
    def __init__(self, bookmaker_name='kambi', headless=True):
        super().__init__(bookmaker_name, headless)
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                if 'betoffer' in url_l or 'listview' in url_l or 'offering' in url_l or 'event' in url_l:
                    data = await response.json()
                    str_data = str(data).lower()
                    
                    if 'events' in str_data or 'liveevents' in str_data and 'betoffers' in str_data and ('outcomes' in str_data or 'mainbetoffer' in str_data):
                        self.raw_data = data
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)
        
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(10000)
        except Exception:
            pass
            
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_data:
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_data, f, indent=4, ensure_ascii=False)

        await self._parse_kambi_data()

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

    async def _parse_kambi_data(self):
        if not self.raw_data:
            return

        try:
            events_raw = self.raw_data.get("events", [])
            bet_offers_raw = self.raw_data.get("betOffers", [])
            
            offers, events_map = self._extract_offers_from_events(events_raw)
            offers.extend(bet_offers_raw)
            
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
                    home_team, away_team = name_str, "Adversario"
                    for sep in [" - ", " v ", " vs "]:
                        if sep in name_str:
                            home_team, away_team = name_str.split(sep, 1)
                            break
                
                criterion = offer.get("criterion", {})
                bet_offer_type = offer.get("betOfferType", {})
                
                market_label = criterion.get("englishLabel") or criterion.get("label", "")
                type_name = bet_offer_type.get("englishName") or bet_offer_type.get("name", "")
                market_str = f"{market_label} {type_name}".upper()
                
                if not any(m in market_str for m in ["MATCH ODDS", "FULL TIME", "1X2", "TEMPO REGULAMENTAR"]):
                    continue 
                
                offer_tags = offer.get("tags", [])
                
                is_super_odd = any(tag in offer_tags for tag in ["PRICE_BOOST", "BOOSTED", "ODDS_BOOST"])
                has_early_payout = any(tag in offer_tags for tag in ["EARLY_PAYOUT"]) or "EARLY PAYOUT" in market_str or "GANHO ANTECIPADO" in market_str
                
                outcomes = offer.get("outcomes", [])
                for outcome in outcomes:
                    odd_raw = outcome.get("odds", 0)
                    if odd_raw <= 1000: 
                        continue
                        
                    label_str = (outcome.get("englishLabel") or outcome.get("label", "")).upper()
                    odd_value = odd_raw / 1000.0
                    
                    await self.process_and_store_odd(
                        match_id=event_id,
                        home_team=home_team.strip(),
                        away_team=away_team.strip(),
                        selection_name=label_str,
                        odd_value=odd_value,
                        has_early_payout=has_early_payout,
                        is_super_odd=is_super_odd
                    )
        except Exception:
            pass