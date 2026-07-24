from sqlalchemy import text
from config.database import engine

def create_materialized_views():
    with engine.connect() as conn:
        sql_view = text("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS latest_odds_view AS
        SELECT DISTINCT ON (match_id, bookmaker_id, market, selection)
            id,
            match_id,
            bookmaker_id,
            market,
            selection,
            odd_value,
            created_at
        FROM odds_history
        ORDER BY match_id, bookmaker_id, market, selection, created_at DESC;
        """)
        conn.execute(sql_view)
        
        sql_index = text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_latest_odds_unique 
        ON latest_odds_view (match_id, bookmaker_id, market, selection);
        """)
        conn.execute(sql_index)
        
        conn.commit()
        print("Materialized View 'latest_odds_view' inicializada com sucesso.")