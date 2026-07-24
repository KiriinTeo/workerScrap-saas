from tasks.celery_app import celery_app
from config.database import SessionLocal
from sqlalchemy import text
# from scrapers.betmgm import scrape_betmgm  

@celery_app.task(name="tasks.worker.run_all_scrapers")
def run_all_scrapers():

    print(" [WORKER INICIADO] - Janela de 15 minutos ativada. Disparando robôs...")
    
    # scrape_betmgm.delay()
    # scrape_bet365.delay()
    # scrape_pinnacle.delay()
    
    print("Tarefas de coleta enviadas para a fila com sucesso!")

@celery_app.task(name="tasks.worker.process_betmgm_data")
def process_betmgm_data():
    db = SessionLocal()
    try:
        print("Iniciando extração da BetMGM...")
        
        # 1. get_or_create_bookmaker(...)
        # 2. get_or_create_match(...)
        # 3. bulk_insert_odds(...)
        
    except Exception as e:
        print(f"Erro na BetMGM: {e}")
    finally:
        db.close() 

def refresh_materialized_views():
    db = SessionLocal()
    try:
        db.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY latest_odds_view"))
        db.commit()
        print("Materialized View atualizada com as novas odds!")
    except Exception as e:
        print(f"Erro ao atualizar View: {e}")
    finally:
        db.close()