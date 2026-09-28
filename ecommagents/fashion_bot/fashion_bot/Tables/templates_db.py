import logging
from typing import Optional, Dict, Any, List
from fashion_bot.database_manager import get_postgres_cursor

logger = logging.getLogger(__name__)

DDL_TEMPLATES = """
CREATE TABLE IF NOT EXISTS gupshup_templates (
  id SERIAL PRIMARY KEY,
  client_id VARCHAR(128) NOT NULL,
  event_key VARCHAR(128) NOT NULL,
  template_id VARCHAR(128) NOT NULL,
  image_url TEXT,
  param_order TEXT, -- comma-separated
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (client_id, event_key)
);
"""

DDL_TEMPLATE_META = """
CREATE TABLE IF NOT EXISTS gupshup_template_catalog (
  id SERIAL PRIMARY KEY,
  client_id VARCHAR(128) NOT NULL,
  name VARCHAR(255) NOT NULL,
  language_code VARCHAR(32) NOT NULL,
  category VARCHAR(64) NOT NULL,
  template_id VARCHAR(128), -- returned by Gupshup after approval
  raw_definition JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (client_id, name, language_code)
);
"""

def ensure_tables():
    conn, cur = get_postgres_cursor()
    try:
        cur.execute(DDL_TEMPLATES)
        cur.execute(DDL_TEMPLATE_META)
        conn.commit()
    finally:
        cur.close(); conn.close()

def upsert_client_template(client_id: str, event_key: str, template_id: str, image_url: Optional[str], param_order: List[str]) -> bool:
    """Upsert template for a client"""
    # Ensure client_id is a string (convert UUID if needed)
    client_id_str = str(client_id) if client_id else None
    
    conn, cur = get_postgres_cursor()
    try:
        cur.execute(
            """
            INSERT INTO gupshup_templates(client_id, event_key, template_id, image_url, param_order)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (client_id, event_key)
            DO UPDATE SET template_id = EXCLUDED.template_id, image_url = EXCLUDED.image_url, param_order = EXCLUDED.param_order
            """,
            (client_id_str, event_key, template_id, image_url, ",".join(param_order or []))
        )
        conn.commit(); return True
    except Exception as e:
        logger.error(f"Upsert client template error: {e}")
        conn.rollback(); return False
    finally:
        cur.close(); conn.close()

def get_client_template(client_id: str, event_key: str) -> Optional[Dict[str, Any]]:
    """Get template for a client"""
    # Ensure client_id is a string (convert UUID if needed)
    client_id_str = str(client_id) if client_id else None
    
    conn, cur = get_postgres_cursor()
    try:
        cur.execute(
            "SELECT template_id, image_url, param_order FROM gupshup_templates WHERE client_id=%s AND event_key=%s",
            (client_id_str, event_key)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            'template_id': row['template_id'],
            'image_url': row['image_url'],
            'param_order': row['param_order'].split(',') if row['param_order'] else []
        }
    finally:
        cur.close(); conn.close()

# Backward compatibility aliases
upsert_tenant_template = upsert_client_template
get_tenant_template = get_client_template

def upsert_client_template_catalog(client_id: str, name: str, language_code: str, category: str, template_id: Optional[str], raw_definition: Dict[str, Any]) -> bool:
    """Upsert template catalog for a client"""
    # Ensure client_id is a string (convert UUID if needed)
    client_id_str = str(client_id) if client_id else None
    
    conn, cur = get_postgres_cursor()
    try:
        cur.execute(
            """
            INSERT INTO gupshup_template_catalog(client_id, name, language_code, category, template_id, raw_definition)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_id, name, language_code)
            DO UPDATE SET category = EXCLUDED.category, template_id = EXCLUDED.template_id, raw_definition = EXCLUDED.raw_definition
            """,
            (client_id_str, name, language_code, category, template_id, raw_definition)
        )
        conn.commit(); return True
    except Exception as e:
        logger.error(f"Upsert client template catalog error: {e}")
        conn.rollback(); return False
    finally:
        cur.close(); conn.close()

def get_client_template_catalog(client_id: str, name: str, language_code: str) -> Optional[Dict[str, Any]]:
    """Get template catalog for a client"""
    # Ensure client_id is a string (convert UUID if needed)
    client_id_str = str(client_id) if client_id else None
    
    conn, cur = get_postgres_cursor()
    try:
        cur.execute(
            "SELECT client_id, name, language_code, category, template_id, raw_definition FROM gupshup_template_catalog WHERE client_id=%s AND name=%s AND language_code=%s",
            (client_id_str, name, language_code)
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        cur.close(); conn.close()

# Backward compatibility aliases
upsert_template_catalog = upsert_client_template_catalog
get_template_catalog = get_client_template_catalog 