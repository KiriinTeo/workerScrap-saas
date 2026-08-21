import json
import traceback
from .base_scraper import BaseScraper

class KambiScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('kto', headless)
        self.raw_payloads = []

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                url_l = response.url.lower()
                if 'offering' in url_l or 'betoffer' in url_l or 'event' in url_l or 'listview' in url_l or 'api' in url_l:
                    data = await response.json()
                    str_data = str(data)
                    if 'events' in str_data and 'betoffers' in str_data.lower():
                        self.raw_payloads.append(data)
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_payloads = []
        self.page.on("response", self.intercept_odds)

        await self.page.goto(url, wait_until="domcontentloaded")
        await self.page.evaluate("window.scrollBy(0, 1000)")
        await self.page.wait_for_timeout(8000)
        
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_payloads:
            import os
            os.makedirs("dumps", exist_ok=True)
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_payloads, f, indent=2, ensure_ascii=False)

        if self.raw_payloads:
            await self._parse_kambi_data(self.raw_payloads)

    async def _parse_kambi_data(self, payloads):
        try:
            processed_ids = set()
            all_items = []

            for data in payloads:
                if isinstance(data, dict):
                    events_list = data.get("events", [])
                    if isinstance(events_list, list):
                        all_items.extend(events_list)

            for item in all_items:
                if not isinstance(item, dict):
                    continue

                event_info = item.get("event")
                if not isinstance(event_info, dict):
                    continue

                match_id = event_info.get("id")
                if not match_id or match_id in processed_ids:
                    continue

                processed_ids.add(match_id)

                home_team = event_info.get("homeName")
                away_team = event_info.get("awayName")

                if not home_team or not away_team:
                    name_str = event_info.get("name", "")
                    if " - " in name_str:
                        parts = name_str.split(" - ", 1)
                        if len(parts) == 2:
                            home_team, away_team = parts[0].strip(), parts[1].strip()

                if not home_team or not away_team:
                    continue

                bet_offers = item.get("betOffers", [])
                if not isinstance(bet_offers, list):
                    continue

                for offer in bet_offers:
                    if not isinstance(offer, dict):
                        continue

                    criterion = offer.get("criterion", {})
                    bet_offer_type = offer.get("betOfferType", {})

                    if not isinstance(criterion, dict) or not isinstance(bet_offer_type, dict):
                        continue

                    market_label = str(criterion.get("englishLabel", "") or criterion.get("label", "")).upper()
                    type_name = str(bet_offer_type.get("englishName", "") or bet_offer_type.get("name", "")).upper()
                    market_str = f"{market_label} {type_name}"

                    is_main_market = any(m in market_str for m in ["MATCH", "FULL TIME", "1X2", "RESULTADO"])
                    
                    offer_tags = offer.get("tags", [])
                    if not isinstance(offer_tags, list):
                        offer_tags = []
                    
                    offer_tags_upper = [str(t).upper() for t in offer_tags]
                    is_super_odd_market = any(tag in offer_tags_upper for tag in ["PRICE_BOOST", "BOOSTED", "ODDS_BOOST"])

                    if not is_main_market and not is_super_odd_market:
                        continue

                    has_ep_market = any(tag in offer_tags_upper for tag in ["EARLY_PAYOUT", "EP"]) or "EARLY PAYOUT" in market_str

                    outcomes = offer.get("outcomes", [])
                    if not isinstance(outcomes, list):
                        continue

                    for outcome in outcomes:
                        if not isinstance(outcome, dict):
                            continue

                        odd_raw = outcome.get("odds", 0)
                        if not odd_raw or odd_raw <= 1000:
                            continue

                        odd_value = odd_raw / 1000.0

                        label_str = str(outcome.get("englishLabel", "") or outcome.get("label", "")).upper()
                        type_str = str(outcome.get("type", "")).upper()

                        selection = None
                        if type_str == "OT_ONE" or label_str in ["1", home_team.upper()]:
                            selection = "1"
                        elif type_str == "OT_CROSS" or label_str in ["X", "DRAW", "EMPATE"]:
                            selection = "X"
                        elif type_str == "OT_TWO" or label_str in ["2", away_team.upper()]:
                            selection = "2"

                        if not selection:
                            continue

                        is_vitoria = selection in ["1", "2"]
                        
                        has_early_payout = False
                        if is_vitoria and (has_ep_market or is_main_market):
                            has_early_payout = True

                        await self.process_and_store_odd(
                            match_id=match_id,
                            home_team=home_team,
                            away_team=away_team,
                            selection_name=label_str,
                            odd_value=odd_value,
                            has_early_payout=has_early_payout,
                            is_super_odd=is_super_odd_market
                        )

        except Exception:
            traceback.print_exc()