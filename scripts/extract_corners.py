import os
import sys
import json
import argparse
import urllib.request
import urllib.error

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PredictF1/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def extract_session_corners(season: int, round_num: int):
    print(f"Fetching {season} Round {round_num} from OpenF1 API...")
    
    # 1. Find Meeting Key
    meetings = fetch_json(f"https://api.openf1.org/v1/meetings?year={season}")
    if not meetings:
        print("Failed to fetch season calendar.")
        return None

    # Filter meetings (skip pre-season testing)
    gp_meetings = [m for m in meetings if "testing" not in m.get("meeting_name", "").lower()]
    gp_meetings.sort(key=lambda x: x.get("date_start", ""))

    if round_num > len(gp_meetings):
        print(f"Round {round_num} does not exist in {season} calendar.")
        return None

    meeting = gp_meetings[round_num - 1]
    meeting_key = meeting["meeting_key"]
    circuit_key = meeting.get("circuit_key")
    event_name = meeting.get("meeting_name", f"Round {round_num}")
    print(f"✓ Found Event: {event_name} (Meeting Key: {meeting_key})")

    # 2. Fetch Circuit Corners from MultiViewer
    corners_url = meeting.get("circuit_info_url") or f"https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{season}"
    circuit_data = fetch_json(corners_url)
    corners = circuit_data.get("corners", []) if circuit_data else []
    print(f"✓ Circuit has {len(corners)} corners")

    # 3. Find Race Session Key
    sessions = fetch_json(f"https://api.openf1.org/v1/sessions?meeting_key={meeting_key}")
    if not sessions:
        print("No sessions found for this meeting.")
        return None

    race_sess = next((s for s in sessions if s.get("session_type", "").lower() == "race" or "race" in s.get("session_name", "").lower()), None)
    if not race_sess:
        print("Race session not found yet.")
        return None

    session_key = race_sess["session_key"]
    print(f"✓ Found Race Session: {session_key}")

    # 4. Fetch Drivers
    drivers_data = fetch_json(f"https://api.openf1.org/v1/drivers?session_key={session_key}")
    if not drivers_data:
        print("No driver data available.")
        return None

    results = {
        "season": season,
        "round": round_num,
        "event_name": event_name,
        "session": "Race",
        "total_corners": len(corners),
        "drivers": {}
    }

    # Extract fastest lap & corner metrics for top drivers
    for d in drivers_data[:10]: # Top drivers for lightweight payload
        d_num = d.get("driver_number")
        code = d.get("name_acronym") or str(d_num)
        
        # Get driver laps
        laps = fetch_json(f"https://api.openf1.org/v1/laps?session_key={session_key}&driver_number={d_num}")
        if not laps:
            continue

        valid_laps = [l for l in laps if l.get("lap_duration") and not l.get("is_pit_out_lap")]
        if not valid_laps:
            continue

        fastest = min(valid_laps, key=lambda x: x["lap_duration"])
        start_time = fastest.get("date_start")
        
        # Fetch car telemetry for this lap
        car_url = f"https://api.openf1.org/v1/car_data?session_key={session_key}&driver_number={d_num}&date>={start_time}"
        car_points = fetch_json(car_url)
        if not car_points:
            continue

        # Slice ~100 points along the lap
        lap_duration = fastest["lap_duration"]
        lap_points = car_points[:min(len(car_points), int(lap_duration * 4))] # 4Hz

        # Map corners
        driver_corners = []
        step = max(1, len(lap_points) // max(1, len(corners)))
        for idx, c in enumerate(corners):
            c_num = c.get("number", idx + 1)
            p_idx = min(idx * step, len(lap_points) - 1)
            pt = lap_points[p_idx]

            driver_corners.append({
                "turn": c_num,
                "apex_speed_kmh": pt.get("speed"),
                "gear": pt.get("n_gear"),
                "brake": pt.get("brake"),
                "throttle": pt.get("throttle")
            })

        results["drivers"][code] = {
            "driver_number": d_num,
            "fastest_lap": fastest.get("lap_number"),
            "lap_time": fastest.get("lap_duration"),
            "corners": driver_corners
        }
        print(f"✓ Processed {code} (Lap {fastest.get('lap_number')})")

    # Save output
    out_dir = os.path.join("telemetry", str(season), f"{round_num:02d}")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "corners.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\n Saved {len(results['drivers'])} drivers to {out_path} ({os.path.getsize(out_path)} bytes)")
    return out_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=str, default="2024")
    parser.add_argument("--round", type=str, default="2")
    args = parser.parse_args()

    s_year = int(args.season)
    r_num = int(args.round) if args.round != "latest" else 1
    extract_session_corners(s_year, r_num)
