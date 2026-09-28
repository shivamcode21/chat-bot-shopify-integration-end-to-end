"""
Local Template Store - Alternative to Gupshup API fetching
Stores template definitions in database for rendering without API calls
"""

import logging
from typing import Optional, Dict, Any, List
from fashion_bot.database_manager import get_postgres_cursor

logger = logging.getLogger(__name__)


def ensure_template_definitions_table():
    """Create template_definitions table if it doesn't exist"""
    conn = None
    cur = None
    try:
        conn, cur = get_postgres_cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS template_definitions (
                id SERIAL PRIMARY KEY,
                client_id VARCHAR(128) NOT NULL,
                template_id VARCHAR(255) NOT NULL,
                template_name VARCHAR(255),
                body_text TEXT NOT NULL,
                header_format VARCHAR(50),
                footer_text TEXT,
                param_count INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (client_id, template_id)
            );
        """)
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_template_definitions_client_template
            ON template_definitions (client_id, template_id);
        """)
        
        logger.info("✅ Template definitions table ensured")
        return True
    except Exception as e:
        logger.error(f"Error creating template_definitions table: {e}")
        return False
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass


def upsert_template_definition(
    client_id: str,
    template_id: str,
    body_text: str,
    template_name: Optional[str] = None,
    header_format: Optional[str] = None,
    footer_text: Optional[str] = None,
    param_count: int = 0
) -> bool:
    """
    Store or update a template definition locally.
    
    Args:
        client_id: Client ID
        template_id: Gupshup template ID
        body_text: Template body text with placeholders (e.g., "Hello {{1}}, order {{2}} delivered")
        template_name: Human-readable name
        header_format: Header format (IMAGE, TEXT, VIDEO, etc.)
        footer_text: Footer text
        param_count: Number of parameters in template
        
    Returns:
        True if successful, False otherwise
        
    Example:
        upsert_template_definition(
            client_id="default",
            template_id="a93171f9-31ab-4e63-8c32-5de54baabe9b",
            body_text="Hello {{1}}, your order {{2}} has been delivered!",
            template_name="Order Delivered",
            header_format="IMAGE",
            footer_text="Thank you for shopping with us",
            param_count=2
        )
    """
    conn = None
    cur = None
    try:
        conn, cur = get_postgres_cursor()
        cur.execute("""
            INSERT INTO template_definitions 
            (client_id, template_id, template_name, body_text, header_format, footer_text, param_count, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (client_id, template_id)
            DO UPDATE SET
                template_name = EXCLUDED.template_name,
                body_text = EXCLUDED.body_text,
                header_format = EXCLUDED.header_format,
                footer_text = EXCLUDED.footer_text,
                param_count = EXCLUDED.param_count,
                updated_at = NOW()
        """, (
            str(client_id),
            template_id,
            template_name,
            body_text,
            header_format,
            footer_text,
            param_count
        ))
        
        logger.info(f"✅ Template definition stored: {template_id} ({template_name})")
        return True
        
    except Exception as e:
        logger.error(f"Error storing template definition: {e}")
        return False
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass


def get_local_template_definition(client_id: str, template_id: str) -> Optional[Dict[str, Any]]:
    """
    Get template definition from local database.
    
    Args:
        client_id: Client ID
        template_id: Gupshup template ID
        
    Returns:
        Template definition dict compatible with render_template_message()
    """
    conn = None
    cur = None
    try:
        conn, cur = get_postgres_cursor()
        cur.execute("""
            SELECT template_name, body_text, header_format, footer_text, param_count
            FROM template_definitions
            WHERE client_id = %s AND template_id = %s
        """, (str(client_id), template_id))
        
        row = cur.fetchone()
        
        if not row:
            return None
        
        # Convert to format compatible with render_template_message()
        components = []
        
        # Add header if present
        if row['header_format']:
            components.append({
                'type': 'HEADER',
                'format': row['header_format'].upper()
            })
        
        # Add body
        components.append({
            'type': 'BODY',
            'text': row['body_text']
        })
        
        # Add footer if present
        if row['footer_text']:
            components.append({
                'type': 'FOOTER',
                'text': row['footer_text']
            })
        
        template_def = {
            'id': template_id,
            'elementName': row['template_name'] or template_id,
            'components': components
        }
        
        logger.info(f"[LOCAL_TEMPLATE] Found template definition for {template_id}")
        return template_def
        
    except Exception as e:
        logger.error(f"Error fetching local template definition: {e}")
        return None
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass


def render_from_local_template(client_id: str, template_id: str, params: List[str]) -> Optional[str]:
    """
    Render a template message using local template definition.
    
    Args:
        client_id: Client ID
        template_id: Gupshup template ID
        params: List of parameter values
        
    Returns:
        Rendered message text or None if template not found
        
    Example:
        message = render_from_local_template(
            client_id="default",
            template_id="a93171f9-31ab-4e63-8c32-5de54baabe9b",
            params=["John", "ORD123"]
        )
        # Returns: "Hello John, your order ORD123 has been delivered!"
    """
    template_def = get_local_template_definition(client_id, template_id)
    
    if not template_def:
        return None
    
    # Use the existing render function
    try:
        from fashion_bot.utils.gupshup_api_client import render_template_message
        return render_template_message(template_def, params)
    except Exception as e:
        logger.error(f"Error rendering template: {e}")
        return None


def list_templates(client_id: str) -> List[Dict[str, Any]]:
    """List all template definitions for a client"""
    conn = None
    cur = None
    try:
        conn, cur = get_postgres_cursor()
        cur.execute("""
            SELECT template_id, template_name, body_text, param_count, updated_at
            FROM template_definitions
            WHERE client_id = %s
            ORDER BY template_name
        """, (str(client_id),))
        
        results = []
        for row in cur.fetchall():
            results.append({
                'template_id': row['template_id'],
                'template_name': row['template_name'],
                'body_text': row['body_text'],
                'param_count': row['param_count'],
                'updated_at': row['updated_at'].isoformat() if row['updated_at'] else None
            })
        
        return results
        
    except Exception as e:
        logger.error(f"Error listing templates: {e}")
        return []
    finally:
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass


# Initialize table on import
ensure_template_definitions_table()
