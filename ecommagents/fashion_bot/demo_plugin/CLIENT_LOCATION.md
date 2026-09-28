# Demo Client Location Flow

This applies only to the demo Chrome extension chat flow.

## Frontend Payload

`chrome_extension/content.js` sends `clientLocation` with every `/demo/chat` and `/demo/chat/stream` request.

When browser geolocation is allowed, it sends:

```json
{
  "permissionStatus": "granted",
  "latitude": 28.6129,
  "longitude": 77.2295,
  "accuracy": 42,
  "timezone": "Asia/Kolkata",
  "locale": "en-IN",
  "capturedAt": "2026-06-11T09:00:00.000Z"
}
```

The extension contains a public IP-based fallback path, but it is currently disabled:

```javascript
const ENABLE_IP_LOCATION_FALLBACK = false;
```

When enabled in future, it can add:

```json
{
  "publicIp": "182.77.77.15",
  "publicCity": "New Delhi",
  "publicPincode": "110003",
  "publicRegion": "National Capital Territory of Delhi",
  "publicCountry": "India",
  "publicCountryCode": "IN",
  "publicLatitude": 28.6327,
  "publicLongitude": 77.2198,
  "publicTimezone": "Asia/Kolkata",
  "publicIsp": "Bharti Airtel"
}
```

## Backend Resolution

The backend uses `fashion_bot.utils.client_location.resolve_client_location_for_demo`.

Resolution order:

1. If browser `latitude` and `longitude` are valid, call Nominatim reverse geocoding:
   `https://nominatim.openstreetmap.org/reverse?lat=<lat>&lon=<lon>&format=jsonv2`
2. Extract city from `address.city`, falling back to `town`, `municipality`, `county`, or `state_district`.
3. Extract pincode from `address.postcode`.
4. If browser lat/lon is unavailable, location state is marked unavailable.

The backend also keeps the IP fallback normalization code behind:

```shell
DEMO_IP_LOCATION_FALLBACK_ENABLED=false
```

With the current default, IP-derived location fields are ignored.

## Request State

The resolved value is stored per request at:

```python
http_request.state.location
```

State fields currently set:

```json
{
  "city": "New Delhi",
  "pincode": "110003",
  "state": "Delhi",
  "country": "India",
  "country_code": "in",
  "backend_ip": "127.0.0.1",
  "latitude": 28.6129,
  "longitude": 77.2295,
  "accuracy_meters": 42,
  "source": "browser_geolocation",
  "city_source": "nominatim",
  "permission_status": "granted",
  "timezone": "Asia/Kolkata",
  "locale": "en-IN",
  "captured_at": "2026-06-11T09:00:00.000Z",
  "display_name": "India Gate, Shahjahan Road, New Delhi, Delhi, India",
  "name": "India Gate"
}
```

`backend_ip` is the IP FastAPI sees from headers/socket. In local development this can be `127.0.0.1`. It is kept for debugging only and is not used for city/pincode resolution while IP fallback is disabled.

This state is request-scoped and is not persisted across chat requests.

## LLM Context State

The demo chat preparation also copies the useful location fields into the LLM-side context object:

```python
shopify_data["client_location"]
```

Current fields:

```json
{
  "city": "New Delhi",
  "pincode": "110003",
  "state": "Delhi",
  "country": "India",
  "country_code": "in",
  "latitude": 28.6129,
  "longitude": 77.2295
}
```

Future prompt logic can read from:

```python
location = shopify_data.get("client_location") or {}
city = location.get("city")
pincode = location.get("pincode")
```
