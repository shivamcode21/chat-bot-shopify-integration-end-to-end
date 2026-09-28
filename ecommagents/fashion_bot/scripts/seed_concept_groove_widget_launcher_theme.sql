INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
    'c3ffcb1b-afb9-4ca4-8746-a06698bec870'::uuid,
    'widget_launcher_theme',
    '{
      "launcher_theme": {
        "background": "linear-gradient(135deg, #6F4A52 0%, #9C6B73 50%, #D7A7A0 100%)",
        "text_color": "rgba(255,255,255,0.96)",
        "caret_color": "rgba(255,255,255,0.9)",
        "ring_color": "rgba(215,167,160,0.72)",
        "shadow": "0 8px 28px rgba(79, 48, 56, 0.30)",
        "avatar_background": "rgba(255,255,255,0.16)",
        "dot_border_color": "#7d5861"
      }
    }'::jsonb
)
ON CONFLICT (client_id, config_key)
DO UPDATE SET config_value = EXCLUDED.config_value;
