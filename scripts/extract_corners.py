import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

def fetch_json(url: str, retries: int = 4):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PredictF1/1.0)"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_time = 2.5 * (attempt + 1)
                print(f"⏳ OpenF1 Rate limit (429). Pausing {wait_time}s before retry...")
                time.sleep(wait_time)
            else:
                return None
        except Exception:
            time.sleep(1.0)
    return None

def extract_single_round(season: int, round_num: int, meeting: dict, session_name_filter: str = "Race", force: bool = False):
    meeting_key = meeting["meeting_key"]
    circuit_key = meeting.get("circuit_key")
    event_name = meeting.get("meeting_name", f"Round {round_num}")
    
    out_dir = os.path.join("telemetry", str(season), f"{round_num:02d}")
    out_path = os.path.join(out_dir, "corners.json")

    # ⚡ SMART CHECK: If already extracted, skip immediately
    if os.path.exists(out_path) and os.path.getsize(out_path) > 500 and not force:
        print(f"⏩ Skipping {season} Round {round_num:02d} ({event_name}): Already extracted!")
        return True

    print(f"\n=======================================================")
    print(f"🏁 Processing: {season} Round {round_num:02d} ({event_name})")
    print(f"=======================================================")

    # 1. Fetch Circuit Corners
    corners_url = meeting.get("circuit_info_url") or f"https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{season}"
    circuit_data = fetch_json(corners_url)
    corners = circuit_data.get("corners", []) if circuit_data else []

    # 2. Find Requested Session
    sessions = fetch_json(f"https://api.openf1.org/v1/sessions?meeting_key={meeting_key}")
    if not sessions:
        print(f"⏭ Skipping: No sessions found yet for {event_name}")
        return False

    target_sess = next((s for s in sessions if session_name_filter.lower() in s.get("session_name", "").lower()), None)
    if not target_sess:
        print(f"⏭ Skipping: Session '{session_name_filter}' not found or not run yet.")
        return False

    session_key = target_sess["session_key"]
    print(f"✓ Found Session: {target_sess.get('session_name')} (Key: {session_key})")

    # 3. Fetch Drivers
    drivers_data = fetch_json(f"https://api.openf1.org/v1/drivers?session_key={session_key}")
    if not drivers_data:
        print(f"⏭ Skipping: No driver data yet.")
        return False

    results = {
        "season": season,
        "round": round_num,
        "event_name": event_name,
        "session": target_sess.get("session_name"),
        "total_corners": len(corners),
        "drivers": {}
    }

    # Extract fastest lap & corner metrics for top 6 drivers
    for d in drivers_data[:6]:
        d_num = d.get("driver_number")
        code = d.get("name_acronym") or str(d_num)
        
        time.sleep(0.3) # ⚡ Safe delay to respect API rate limits
        laps = fetch_json(f"https://api.openf1.org/v1/laps?session_key={session_key}&driver_number={d_num}")
        if not laps:
            continue

        valid_laps = [l for l in laps if l.get("lap_duration") and not l.get("is_pit_out_lap")]
        if not valid_laps:
            continue

        fastest = min(valid_laps, key=lambda x: x["lap_duration"])
        start_time = fastest.get("date_start")
        
        time.sleep(0.3) # ⚡ Safe delay
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
        print(f"  ✓ {code} (Lap {fastest.get('lap_number')})")

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
    parser.add_argument("--season", type=str, default="2024")
    parser.add_argument("--round", type=str, default="all")
    parser.add_argument("--session", type=str, default="Race")
    parser.add_argument("--force", action="store_true", help="Force overwrite existing files")
    args = parser.parse_args()

    s_year = int(args.season)
    
    print(f"📅 Fetching {s_year} Season Calendar...")
    meetings = fetch_json(f"https://api.openf1.org/v1/meetings?year={s_year}")
    if not meetings:
        print("Failed to fetch calendar.")
        return

    gp_meetings = [m for m in meetings if "testing" not in m.get("meeting_name", "").lower()]
    gp_meetings.sort(key=lambda x: x.get("date_start", ""))

    print(f"Found {len(gp_meetings)} Grand Prix events in {s_year}.")

    if args.round.lower() == "all":
        rounds_to_process = list(range(1, len(gp_meetings) + 1))
    else:
        rounds_to_process = [int(args.round)]

    successful = 0
    for r in rounds_to_process:
        if r <= len(gp_meetings):
            meeting = gp_meetings[r - 1]
            if extract_single_round(s_year, r, meeting, args.session, force=args.force):
                successful += 1

    print(f"\n🎉 Finished! Processed {successful}/{len(rounds_to_process)} rounds.")

if __name__ == "__main__":
    main()
