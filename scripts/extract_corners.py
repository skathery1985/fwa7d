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
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = 3.5 * (attempt + 1)
                print(f"⏳ OpenF1 Rate limit (429). Pausing {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                return None
        except Exception:
            time.sleep(1.0)
    return None

def get_official_schedule(season: int):
    """Fetches official championship rounds from Ergast/Jolpi."""
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
    """Matches an official FIA calendar round to OpenF1 meeting."""
    r_name = official_race["race_name"].lower()
    r_circuit = official_race["circuit_id"].lower()
    r_date = official_race.get("date", "")

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

    for m in openf1_meetings:
        m_name = (m.get("meeting_name") or "").lower()
        m_circuit = (m.get("circuit_short_name") or "").lower()
        if r_name in m_name or m_name in r_name:
            return m
        if r_circuit and (r_circuit in m_circuit or r_circuit in m_name):
            return m

    keywords = ["baku", "monza", "silverstone", "monaco", "spa", "suzuka", "miami", "melbourne", "shanghai", "zandvoort", "hungaroring", "catalunya", "austria", "mexico", "interlagos", "vegas", "losail", "yas marina"]
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
    
    round_dir = os.path.join("telemetry", str(season), f"{official_round:02d}")
    laps_dir = os.path.join(round_dir, "laps")
    index_path = os.path.join(round_dir, "corners.json")

    if os.path.exists(index_path) and os.path.exists(laps_dir) and len(os.listdir(laps_dir)) > 10 and not force:
        print(f"⏩ Skipping {season} Round {official_round:02d} ({event_name}): Already extracted!")
        return True

    print(f"\n=======================================================")
    print(f"🏁 Processing ALL LAPS & CORNERS: {season} Round {official_round:02d} - {event_name}")
    print(f"=======================================================")

    # 1. Fetch Circuit Corners
    corners_url = meeting.get("circuit_info_url") or f"https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{season}"
    circuit_data = fetch_json(corners_url)
    corners = circuit_data.get("corners", []) if circuit_data else []
    total_corners = len(corners)
    print(f"✓ Found {total_corners} corners for circuit {circuit_key}")

    # 2. Find Requested Session
    sessions = fetch_json(f"https://api.openf1.org/v1/sessions?meeting_key={meeting_key}")
    if not sessions:
        print(f"⏭ Skipping: No sessions found.")
        return False

    target_sess = next((s for s in sessions if session_name_filter.lower() in s.get("session_name", "").lower()), None)
    if not target_sess:
        print(f"⏭ Skipping: Session '{session_name_filter}' not found.")
        return False

    session_key = target_sess["session_key"]
    print(f"✓ Found Session: {target_sess.get('session_name')} (Key: {session_key})")

    # 3. Fetch ALL Drivers
    drivers_data = fetch_json(f"https://api.openf1.org/v1/drivers?session_key={session_key}")
    if not drivers_data:
        print(f"⏭ Skipping: No driver data found.")
        return False

    # Structure to hold data: lap_number -> { drivers: { CODE: { corners: [...] } } }
    laps_store = {}
    fastest_benchmark = {}
    available_laps_set = set()

    for d in drivers_data:
        d_num = d.get("driver_number")
        code = d.get("name_acronym") or str(d_num)

        time.sleep(0.35)
        # Fetch all laps for this driver
        driver_laps = fetch_json(f"https://api.openf1.org/v1/laps?session_key={session_key}&driver_number={d_num}")
        if not driver_laps:
            continue

        valid_laps = [l for l in driver_laps if l.get("lap_duration") and l.get("date_start")]
        if not valid_laps:
            continue

        # Fetch entire session car_data in 1 batch
        time.sleep(0.35)
        car_data = fetch_json(f"https://api.openf1.org/v1/car_data?session_key={session_key}&driver_number={d_num}")
        if not car_data:
            continue

        # Fastest lap tracking
        fastest_lap_obj = min(valid_laps, key=lambda x: x["lap_duration"])
        fastest_lap_num = fastest_lap_obj.get("lap_number")

        print(f"  🏎️ Processing Driver: {code} ({len(valid_laps)} laps)")

        # Map car_data by timestamp index for instant lookups
        car_dates = [p["date"] for p in car_data]

        for lap in valid_laps:
            lap_num = lap["lap_number"]
            start_date = lap["date_start"]
            lap_duration = lap["lap_duration"]

            available_laps_set.add(lap_num)

            if lap_num not in laps_store:
                laps_store[lap_num] = {
                    "season": season,
                    "round": official_round,
                    "event_name": event_name,
                    "lap": lap_num,
                    "total_corners": total_corners,
                    "drivers": {}
                }

            # Slice car data for this exact lap
            # Find start index
            idx_start = next((i for i, d_str in enumerate(car_dates) if d_str >= start_date), -1)
            if idx_start == -1:
                continue

            expected_points = int(lap_duration * 3.7) + 5
            lap_points = car_data[idx_start : idx_start + expected_points]
            if not lap_points:
                continue

            driver_corners = []
            step = max(1, len(lap_points) // max(1, total_corners))

            for idx, c in enumerate(corners):
                c_num = c.get("number", idx + 1)
                p_idx = min(idx * step, len(lap_points) - 1)
                pt = lap_points[p_idx]

                driver_corners.append({
                    "turn": c_num,
                    "apex_speed_kmh": pt.get("speed"),
                    "gear": pt.get("n_gear"),
                    "rpm": pt.get("rpm"),
                    "throttle": pt.get("throttle"),
                    "brake": pt.get("brake"),
                    "drs": pt.get("drs") # Active aero mode
                })

            driver_lap_payload = {
                "driver_number": d_num,
                "lap_time": lap_duration,
                "corners": driver_corners
            }

            laps_store[lap_num]["drivers"][code] = driver_lap_payload

            # Save benchmark fastest lap
            if lap_num == fastest_lap_num:
                fastest_benchmark[code] = {
                    "driver_number": d_num,
                    "fastest_lap": fastest_lap_num,
                    "lap_time": lap_duration,
                    "corners": driver_corners
                }

    if not laps_store:
        print("⚠ No valid laps extracted.")
        return False

    # 4. Save per-lap files
    os.makedirs(laps_dir, exist_ok=True)
    sorted_laps = sorted(list(available_laps_set))

    for lap_num, lap_payload in laps_store.items():
        lap_file_path = os.path.join(laps_dir, f"{lap_num:02d}.json")
        with open(lap_file_path, "w", encoding="utf-8") as f:
            json.dump(lap_payload, f, indent=2)

    # 5. Save root corners.json with available laps index + fastest benchmark
    index_payload = {
        "season": season,
        "round": official_round,
        "event_name": event_name,
        "session": target_sess.get("session_name"),
        "total_corners": total_corners,
        "available_laps": sorted_laps,
        "drivers": fastest_benchmark
    }

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_payload, f, indent=2)

    print(f"💾 Saved {len(sorted_laps)} lap files in: {laps_dir}")
    print(f"💾 Saved benchmark index in: {index_path}")
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
            print(f"⚠ Could not match official Round {rnd:02d} ({official_race['race_name']}) in OpenF1.")
            continue

        if extract_single_round(s_year, rnd, matched_meeting, args.session, force=args.force):
            successful += 1

    print(f"\n🎉 Finished! Processed {successful}/{len(rounds_to_process)} rounds.")

if __name__ == "__main__":
    main()
