import os
import sys
import json
import argparse
import fastf1
import pandas as pd

def extract_session_corners(season: int, round_num: int, session_type: str = 'R'):
    print(f"Loading {season} Round {round_num} ({session_type})...")
    
    # Enable FastF1 disk caching
    cache_dir = os.path.join(os.path.expanduser('~'), '.fastf1_cache')
    os.makedirs(cache_dir, exist_ok=True)
    fastf1.Cache.enable_cache(cache_dir)
    
    try:
        session = fastf1.get_session(season, round_num, session_type)
        session.load(laps=True, telemetry=True, weather=False, messages=False)
    except Exception as e:
        print(f"Error loading session: {e}")
        return None

    circuit_info = session.get_circuit_info()
    corners = circuit_info.corners
    if corners.empty:
        print("No corner geometry found for this circuit.")
        return None

    event_name = session.event.get('EventName', f"Round {round_num}")
    drivers_list = session.drivers
    
    results = {
        "season": season,
        "round": round_num,
        "event_name": event_name,
        "session": session_type,
        "total_corners": len(corners),
        "drivers": {}
    }

    for d_num in drivers_list:
        try:
            d_laps = session.laps.pick_drivers(d_num)
            fastest = d_laps.pick_fastest()
            if fastest is None or pd.isna(fastest['LapTime']):
                continue
                
            code = fastest['Driver']
            tel = fastest.get_telemetry()
            if tel.empty or 'Speed' not in tel.columns:
                continue

            driver_corners = []
            for _, c in corners.iterrows():
                c_dist = c['Distance']
                c_num = int(c['Number'])

                # Sample window +- 60 meters from apex
                window = tel[(tel['Distance'] >= c_dist - 60) & (tel['Distance'] <= c_dist + 60)]
                if not window.empty:
                    apex_row = window.loc[window['Speed'].idxmin()]
                    apex_speed = int(apex_row['Speed'])
                    apex_gear = int(apex_row['nGear'])
                    
                    # Brake detection 120m before apex
                    pre_apex = tel[(tel['Distance'] >= c_dist - 120) & (tel['Distance'] <= c_dist)]
                    brakes = pre_apex[pre_apex['Brake'] > 0]
                    brake_dist = round(c_dist - brakes.iloc[0]['Distance'], 1) if not brakes.empty else 0.0

                    # Throttle pickup 120m after apex
                    post_apex = tel[(tel['Distance'] >= c_dist) & (tel['Distance'] <= c_dist + 120)]
                    throt = post_apex[post_apex['Throttle'] >= 95]
                    throttle_pickup = round(throt.iloc[0]['Distance'] - c_dist, 1) if not throt.empty else 0.0
                else:
                    apex_speed = None
                    apex_gear = None
                    brake_dist = 0.0
                    throttle_pickup = 0.0

                driver_corners.append({
                    "turn": c_num,
                    "apex_speed_kmh": apex_speed,
                    "gear": apex_gear,
                    "braking_dist_m": brake_dist,
                    "throttle_pickup_m": throttle_pickup
                })

            results["drivers"][code] = {
                "driver_number": d_num,
                "lap_time": str(fastest['LapTime']),
                "lap_number": int(fastest['LapNumber']),
                "corners": driver_corners
            }
            print(f"✓ Processed {code} (Lap {fastest['LapNumber']})")
        except Exception as err:
            print(f"⚠ Skipping driver {d_num}: {err}")

    # Output file path: telemetry/{season}/{round}/corners.json
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
    parser.add_argument("--session", type=str, default="R")
    args = parser.parse_args()

    s_year = int(args.season)
    r_num = int(args.round) if args.round != "latest" else 1
    extract_session_corners(s_year, r_num, args.session)
