from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from db.models import Bookmaker, Match, OddHistory

def get_or_create_bookmaker(db: Session, name: str, base_url: str):
    bookmaker = db.query(Bookmaker).filter(Bookmaker.name == name).first()
    if not bookmaker:
        bookmaker = Bookmaker(name=name, base_url=base_url)
        db.add(bookmaker)
        db.commit()
        db.refresh(bookmaker)
    return bookmaker

def get_or_create_match(db: Session, home_team: str, away_team: str, league: str, start_time):
    match = db.query(Match).filter(
        Match.home_team == home_team,
        Match.away_team == away_team,
        Match.start_time == start_time
    ).first()
    
    if not match:
        match = Match(home_team=home_team, away_team=away_team, league=league, start_time=start_time)
        db.add(match)
        db.commit()
        db.refresh(match)
    return match

def bulk_insert_odds(db: Session, odds_list: list[dict]):
    if not odds_list:
        return
    
    db.bulk_insert_mappings(OddHistory, odds_list)
    db.commit()
    print(f"✅ {len(odds_list)} novas odds inseridas com sucesso no histórico!")