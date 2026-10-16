import asyncio
from typing import List, Optional
import httpx
from happy_path.config import GOONG_API_KEY, GOONG_BASE_URL, GOONG_TIMEOUT_SEC
from happy_path.core.solver import fmt_clock, parse_departure_minute_and_bucket
from happy_path.schemas.itinerary import Coordinate, ParetoCandidate

class GoongClient:
    """
    High-Concurrency Async Goong REST API V2 Client (Freeze-Spec F8.1 – F8.4):
    - Reuses a persistent httpx.AsyncClient to eliminate per-call TLS/socket construction overhead.
    - Decorates full candidate portfolios concurrently via asyncio.gather inside an outer
      asyncio.wait_for(..., timeout=0.280) wall-clock budget so 3 slow calls always finish < 350ms.
    - Propagates live leg durations into each stop's arrival_clock, service_start_clock, departure_clock,
      total_drive_minutes, total_duration_minutes, and tradeoff_summary_vi.
    - Guards against null or empty overview_polyline.points.
    """
    def __init__(self, api_key: str = GOONG_API_KEY, base_url: str = GOONG_BASE_URL, timeout_sec: float = GOONG_TIMEOUT_SEC):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_sec = min(0.280, max(0.050, timeout_sec))
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_sec)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def decorate_candidate_polyline(
        self,
        origin: Coordinate,
        dest: Coordinate,
        candidate: ParetoCandidate,
        vehicle: str = "car",
        departure_iso: str = "2026-10-16T06:00:00+07:00",
    ) -> ParetoCandidate:
        if not self.api_key:
            return candidate
        waypoints = [
            f"{s.coordinates.lat:.6f},{s.coordinates.lng:.6f}" for s in candidate.stops
        ] + [dest.to_goong_str()]
        params = {
            "origin": origin.to_goong_str(),
            "destination": ";".join(waypoints),
            "vehicle": vehicle,
            "api_key": self.api_key,
        }
        try:
            client = self._get_client()
            resp = await asyncio.wait_for(
                client.get(f"{self.base_url}/direction", params=params),
                timeout=self.timeout_sec,
            )
            if resp.status_code == 200:
                data = resp.json()
                routes: List[dict] = data.get("routes") or []
                if routes:
                    r0 = routes[0]
                    ov = r0.get("overview_polyline")
                    if isinstance(ov, dict):
                        pts = ov.get("points")
                        if isinstance(pts, str) and len(pts.strip()) > 0:
                            candidate.encoded_polyline = pts.strip()

                    legs = r0.get("legs") or []
                    leg_secs = [int((leg.get("duration") or {}).get("value", 0)) for leg in legs]
                    live_secs = sum(leg_secs)
                    if live_secs > 0:
                        live_mins = max(5, live_secs // 60)
                        baseline_drive = candidate.total_drive_minutes
                        if live_mins > int(baseline_drive * 1.35):
                            # Re-propagate stop arrival/service/departure clocks coherently
                            dep_min, _, _ = parse_departure_minute_and_bucket(departure_iso)
                            curr_min = dep_min
                            num_legs = max(1, len(candidate.stops) + 1)
                            if len(leg_secs) == num_legs:
                                per_leg_mins = [max(5, s // 60) for s in leg_secs]
                            else:
                                ratio = live_mins / max(1, baseline_drive)
                                per_leg_mins = [max(5, int(round((live_mins / num_legs)))) for _ in range(num_legs)]
                                _ = ratio

                            for idx, stop in enumerate(candidate.stops):
                                leg_m = per_leg_mins[idx]
                                arr_m = curr_min + leg_m
                                srv_m = arr_m + stop.wait_minutes
                                dep_m = srv_m + stop.dwell_minutes
                                stop.arrival_clock = fmt_clock(arr_m)
                                stop.service_start_clock = fmt_clock(srv_m)
                                stop.departure_clock = fmt_clock(dep_m)
                                curr_min = dep_m

                            candidate.total_drive_minutes = live_mins
                            candidate.total_duration_minutes = (
                                live_mins + candidate.total_dwell_minutes + candidate.total_wait_minutes
                            )
                            stop_names = " -> ".join(s.name_vi for s in candidate.stops)
                            candidate.tradeoff_summary_vi = (
                                f"{candidate.label} [{candidate.active_branch_id}]: {len(candidate.stops)} điểm dừng ({stop_names}), "
                                f"lái xe {live_mins} phút (Goong Live), lệch tuyến +{candidate.total_detour_minutes} phút, "
                                f"phí VETC ~{candidate.estimated_vetc_toll_vnd:,}đ."
                            )
                            candidate.warnings.append(
                                f"Cảnh báo kẹt xe/sự cố trực tiếp từ Goong: Thời gian lái thực tế tăng lên {live_mins} phút (>1.35x chuẩn)."
                            )
        except Exception:
            pass
        return candidate

    async def decorate_portfolio(
        self,
        origin: Coordinate,
        dest: Coordinate,
        candidates: List[ParetoCandidate],
        vehicle: str = "car",
        departure_iso: str = "2026-10-16T06:00:00+07:00",
    ) -> List[ParetoCandidate]:
        if not self.api_key or not candidates:
            return candidates
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        self.decorate_candidate_polyline(origin, dest, cand, vehicle, departure_iso)
                        for cand in candidates
                    ),
                    return_exceptions=True,
                ),
                timeout=self.timeout_sec,
            )
        except Exception:
            pass
        return candidates
