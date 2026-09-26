import os
import sys
import json
import time
import argparse
import datetime
import urllib.request
import urllib.error

def fetch_json(url: str, retries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PredictF1/1.0)"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = 3.0 * (attempt + 1)
                print(f"⏳ OpenF1 Rate limit (429). Pausing {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                return None
        except Exception:
            time.sleep(1.0)
    return None

def get_official_schedule(season: int):
    """Fetches the official FIA calendar rounds from Ergast/Jolpi."""
    url = f"https://api.jolpi.ca/ergast/f1/{season}.json"
    data = fetch_json(url)
    if not data or "MRData" not in data or "RaceTable" not in data["MRData"]:
        return []
    
    races = data["MRData"]["RaceTable"].get("Races", [])
    schedule = []
    for r in races:
        if "round" in r:
            schedule.append({
                "round": int(r["round"]),
                "race_name": r.get("raceName", ""),
                "circuit_id": r.get("Circuit", {}).get("circuitId", ""),
                "date": r.get("date", "")
            })
    return sorted(schedule, key=lambda x: x["round"])

def find_matching_meeting(official_race: dict, openf1_meetings: list):
    """Matches an official calendar round to an OpenF1 meeting."""
    r_name = official_race["race_name"].lower()
    r_circuit = official_race["circuit_id"].lower()
    r_date = official_race.get("date", "")

    # 1. Match by date (within 4 days)
    if r_date:
        for m in openf1_meetings:
            m_date = m.get("date_start", "")[:10]
            if m_date:
                try:
                    dt_race = datetime.date.fromisoformat(r_date)
                    dt_meet = datetime.date.fromisoformat(m_date)
                    if abs((dt_race - dt_meet).days) <= 4:
                        return m
                except Exception:
                    pass

    # 2. Match by exact or partial Grand Prix name / circuit
    for m in openf1_meetings:
        m_name = (m.get("meeting_name") or "").lower()
        m_circuit = (m.get("circuit_short_name") or "").lower()
        if r_name in m_name or m_name in r_name:
            return m
        if r_circuit and (r_circuit in m_circuit or r_circuit in m_name):
            return m

    # 3. Known circuit keyword fallback
    keywords = [
        "baku", "monza", "silverstone", "monaco", "spa", "suzuka", 
        "miami", "melbourne", "shanghai", "zandvoort", "hungaroring", 
        "catalunya", "austria", "mexico", "interlagos", "vegas", "losail", "yas marina"
    ]
    for kw in keywords:
        if kw in r_name:
            for m in openf1_meetings:
                if kw in (m.get("meeting_name") or "").lower() or kw in (m.get("circuit_short_name") or "").lower():
                    return m

    return None

def extract_single_round(season: int, official_round: int, meeting: dict, session_name_filter: str = "Race", force: bool = False):
    meeting_key = meeting["meeting_key"]
    circuit_key = meeting.get("circuit_key")
    event_name = meeting.get("meeting_name", f"Round {official_round}")
    
    out_dir = os.path.join("telemetry", str(season), f"{official_round:02d}")
    out_path = os.path.join(out_dir, "corners.json")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 500 and not force:
        print(f"⏩ Skipping {season} Round {official_round:02d} ({event_name}): Already extracted!")
        return True

    print(f"\n=======================================================")
    print(f"🏁 Processing: {season} Official Round {official_round:02d} - {event_name}")
    print(f"=======================================================")

    # 1. Fetch Circuit Corners
    corners_url = meeting.get("circuit_info_url") or f"https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{season}"
    circuit_data = fetch_json(corners_url)
    corners = circuit_data.get("corners", []) if circuit_data else []

    # 2. Find Requested Session
    sessions = fetch_json(f"https://api.openf1.org/v1/sessions?meeting_key={meeting_key}")
    if not sessions:
        print(f"⏭ Skipping: No sessions found for {event_name}")
        return False

    target_sess = next((s for s in sessions if session_name_filter.lower() in s.get("session_name", "").lower()), None)
    if not target_sess:
        print(f"⏭ Skipping: Session '{session_name_filter}' not found.")
        return False

    session_key = target_sess["session_key"]
    print(f"✓ Found Session: {target_sess.get('session_name')} (Key: {session_key})")

    # 3. Fetch ALL Drivers (no slicing)
    drivers_data = fetch_json(f"https://api.openf1.org/v1/drivers?session_key={session_key}")
    if not drivers_data:
        print(f"⏭ Skipping: No driver data found.")
        return False

    results = {
        "season": season,
        "round": official_round,
        "event_name": event_name,
        "session": target_sess.get("session_name"),
        "total_corners": len(corners),
        "drivers": {}
    }

    # Process all drivers on the grid
    for d in drivers_data:
        d_num = d.get("driver_number")
        code = d.get("name_acronym") or str(d_num)
        
        time.sleep(0.35)  # Safe rate-limit delay
        laps = fetch_json(f"https://api.openf1.org/v1/laps?session_key={session_key}&driver_number={d_num}")
        if not laps:
            continue

        valid_laps = [l for l in laps if l.get("lap_duration") and not l.get("is_pit_out_lap")]
        if not valid_laps:
            continue

        fastest = min(valid_laps, key=lambda x: x["lap_duration"])
        start_time = fastest.get("date_start")
        
        time.sleep(0.35)
        car_url = f"https://api.openf1.org/v1/car_data?session_key={session_key}&driver_number={d_num}&date>={start_time}"
        car_points = fetch_json(car_url)
        if not car_points:
            continue

        lap_duration = fastest["lap_duration"]
        lap_points = car_points[:min(len(car_points), int(lap_duration * 4))]

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
        print(f"  ✓ Extracted driver: {code} (Lap {fastest.get('lap_number')})")

    if not results["drivers"]:
        print(f"⚠ No valid driver laps completed.")
        return False

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"💾 Saved: {out_path} ({os.path.getsize(out_path)} bytes)")
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=str, default="2026")
    parser.add_argument("--round", type=str, default="all")
    parser.add_argument("--session", type=str, default="Race")
    parser.add_argument("--force", action="store_true", help="Force overwrite existing files")
    args = parser.parse_args()

    s_year = int(args.season)

    print(f"📅 Fetching official {s_year} championship schedule...")
    official_schedule = get_official_schedule(s_year)
    if not official_schedule:
        print("Failed to fetch official calendar.")
        return

    print(f"📅 Fetching OpenF1 {s_year} meetings...")
    meetings = fetch_json(f"https://api.openf1.org/v1/meetings?year={s_year}") or []
    gp_meetings = [m for m in meetings if "testing" not in m.get("meeting_name", "").lower()]

    if args.round.lower() == "all":
        rounds_to_process = official_schedule
    else:
        req_round = int(args.round)
        rounds_to_process = [r for r in official_schedule if r["round"] == req_round]

    successful = 0
    for official_race in rounds_to_process:
        rnd = official_race["round"]
        matched_meeting = find_matching_meeting(official_race, gp_meetings)
        
        if not matched_meeting:
            print(f"⚠ Could not match official Round {rnd:02d} ({official_race['race_name']}) in OpenF1 meetings.")
            continue

        if extract_single_round(s_year, rnd, matched_meeting, args.session, force=args.force):
            successful += 1

    print(f"\n🎉 Finished! Processed {successful}/{len(rounds_to_process)} rounds.")

if __name__ == "__main__":
    main()
