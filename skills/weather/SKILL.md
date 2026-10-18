# Weather Skill

Get current weather conditions and forecasts using wttr.in.

## When to Use

**USE this skill when:**
- "What's the weather?"
- "Will it rain today/tomorrow?"
- "Temperature in [city]"
- "Weather forecast for the week"
- "How much rain is expected?"
- Travel planning weather checks

**DON'T use this skill when:**
- Historical weather data → use web_search for archives
- Climate analysis or trends → use specialized data sources
- Severe weather alerts → check official meteorological services

## How to Get Weather

### Step 1: Primary Method - Use browser_navigate with wttr.in

Use the `browser_navigate` tool to access wttr.in directly:

```
browser_navigate("https://wttr.in/<LOCATION>")
```

Replace `<LOCATION>` with the city name, region, or airport code. Examples:
- `https://wttr.in/Berlin`
- `https://wttr.in/London`
- `https://wttr.in/New+York`
- `https://wttr.in/Hamburg`

### Step 2: If First Attempt Fails - Retry Once

If the first `browser_navigate` call fails or returns an error, retry with the same URL once before moving to fallback.

### Step 3: Fallback - Use web_search

After two failed attempts to get weather from wttr.in, use `web_search` to find weather information:

```
web_search("weather forecast <LOCATION> rain amount mm")
```

## Understanding wttr.in Output

The wttr.in page returns ASCII art weather display. Here's how to parse it:

### Current Weather Section (top of output)
```
    \   /    Sunny
     .-.     +12(10) °C
  ― (   ) ―  ↘ 13 km/h
     `-’     10 km
    /   \    0.0 mm
```
- **Conditions**: Sunny, Cloudy, Rain, etc. (shown in ASCII art header)
- **Temperature**: `+12(10) °C` means 12°C current, feels like 10°C
- **Wind**: `↘ 13 km/h` shows wind speed and direction
- **Visibility**: `10 km`
- **Precipitation**: `0.0 mm` shows rain/snow amount in millimeters

### 3-Day Forecast Section

Each day shows hourly data with this format:
```
/ \ 0.0 mm | 0%
```
- **First number**: precipitation amount in millimeters (mm)
- **Second number**: chance of rain as percentage (%)

For each day, look for:
- **Morning / Noon / Evening / Night** blocks
- **Maximum/Minimum temperature** for the day
- **Precipitation** line showing `X.X mm | Y%` where Y is rain chance

### Key Data Points to Extract

| Data | Where to Find |
|------|---------------|
| Current temperature | `+XX °C` format in current section |
| Feels like | `(XX) °C` in current section |
| Conditions | ASCII art symbols (☀️ Sunny, ☁️ Cloudy, 🌧️ Rain, ❄️ Snow) |
| Precipitation | `X.X mm` in current section or forecast cells |
| Rain chance | `X%` after mm value (e.g., `0.0 mm | 0%`) |
| Wind speed | `XX km/h` |
| Humidity | Look for `XX%` near precipitation data |
| 3-day forecast | Separate sections for each day with hourly breakdown |

### Parsing Example

For output containing:
```
/ \ 0.0 mm | 0%
```
This means: **0.0 mm precipitation, 0% chance of rain**

For output containing:
```
/ \ 5.2 mm | 85%
```
This means: **5.2 mm precipitation expected, 85% chance of rain**

## Retry Logic

1. **Attempt 1**: `browser_navigate("https://wttr.in/<location>")`
2. **If fails**: Wait briefly, then retry same URL
3. **If still fails**: Use `web_search("weather <location> rain forecast")` as fallback

## Notes

- wttr.in works for most global cities, towns, and airport codes
- No API key needed
- Include city name, region, or airport code in the URL
- For best results, use `browser_navigate` which renders the full page with all forecast data
