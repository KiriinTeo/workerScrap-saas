import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from config.database import engine, Base
from db.models import Bookmaker, Match, OddHistory 

def configure_advanced_database():
    print("Iniciando configuracao avançada do PostgreSQL...")

    Base.metadata.create_all(bind=engine)
    
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS latest_odds_view AS
            SELECT DISTINCT ON (match_id, bookmaker_id, market, selection)
                id,
                match_id,
                bookmaker_id,
                market,
                selection,
                odd_value,
                is_super_odd,
                collected_at
            FROM odds_history
            ORDER BY match_id, bookmaker_id, market, selection, collected_at DESC;
        """))
        
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_latest_odds_unique 
            ON latest_odds_view (match_id, bookmaker_id, market, selection);
        """))
        
        conn.commit()
        print("Configuracao finalizada com sucesso! Banco pronto para uso.")

if __name__ == "__main__":
    configure_advanced_database()