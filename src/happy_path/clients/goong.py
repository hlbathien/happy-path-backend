import asyncio
import math
from typing import List, Optional
import httpx
from happy_path.config import GOONG_API_KEY, GOONG_BASE_URL, GOONG_TIMEOUT_SEC
from happy_path.core.solver import (
    dominates, fmt_clock, overlaps_midday, parse_departure_minute_and_bucket, service_wait_minutes,
)
from happy_path.schemas.itinerary import Coordinate, ParetoCandidate


class InfeasibleLiveSchedule(Exception):
    """The actual provider legs make this candidate physically infeasible."""


class GoongClient:
    """Persistent, parallel, budget-bounded decoration of the final physical schedule."""
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
        self, origin: Coordinate, dest: Coordinate, candidate: ParetoCandidate,
        vehicle: str = 'car', departure_iso: str = '2026-10-16T06:00:00+07:00',
    ) -> ParetoCandidate:
        if not self.api_key:
            return candidate
        waypoints = [s.coordinates.to_goong_str() for s in candidate.stops] + [dest.to_goong_str()]
        params = {'origin': origin.to_goong_str(), 'destination': ';'.join(waypoints),
                  'vehicle': vehicle, 'api_key': self.api_key}
        try:
            response = await asyncio.wait_for(
                self._get_client().get(f'{self.base_url}/direction', params=params), timeout=self.timeout_sec,
            )
            response.raise_for_status()
            routes = response.json().get('routes') or []
            if not routes:
                raise ValueError('no route')
            route = routes[0]
            legs = route.get('legs') or []
            seconds = [float((leg.get('duration') or {}).get('value', 0)) for leg in legs]
            if len(seconds) != len(candidate.stops) + 1 or any(not math.isfinite(s) or s <= 0 for s in seconds):
                raise ValueError('incomplete live legs')
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
            candidate.warnings.append('GOONG_UNAVAILABLE: Không có dữ liệu hành trình trực tiếp hợp lệ; giữ ước tính ngoại tuyến.')
            return candidate

        # Stage all clocks first. Never leave a partially decorated or invalid candidate behind.
        staged = candidate.model_copy(deep=True)
        minutes = [max(5, math.ceil(s / 60)) for s in seconds]
        clock, _, _ = parse_departure_minute_and_bucket(departure_iso)
        total_wait = 0
        for stop, leg in zip(staged.stops, minutes):
            arrival = clock + leg
            wait = service_wait_minutes(arrival, stop.open_minute, stop.close_minute,
                                        stop.dwell_minutes, stop.is_24h, staged.max_wait_minutes)
            if wait is None:
                raise InfeasibleLiveSchedule(f'LIVE_OPERATING_WINDOW: {stop.poi_id}')
            service = arrival + wait
            if stop.midday_sun_trap and overlaps_midday(service, stop.dwell_minutes):
                if not stop.is_locked:
                    raise InfeasibleLiveSchedule(f'LIVE_UV_WINDOW: {stop.poi_id}')
                if not any('UV_LOCK_OVERRIDE_WARNING' in w for w in staged.warnings):
                    staged.warnings.append('UV_LOCK_OVERRIDE_WARNING: Điểm dừng khóa cố định nằm trong khung giờ nắng gắt 11:00-14:30.')
            clock = service + stop.dwell_minutes
            stop.arrival_clock = fmt_clock(arrival)
            stop.service_start_clock = fmt_clock(service)
            stop.departure_clock = fmt_clock(clock)
            stop.wait_minutes = wait
            total_wait += wait

        live_drive = sum(minutes)
        staged.total_drive_minutes = live_drive
        staged.total_wait_minutes = total_wait
        staged.total_duration_minutes = live_drive + staged.total_dwell_minutes + total_wait
        staged.tradeoff_summary_vi = (
            f'{staged.label} [{staged.active_branch_id}]: {len(staged.stops)} điểm dừng '
            f'({" -> ".join(s.name_vi for s in staged.stops)}), lái xe {live_drive} phút (Goong Live), '
            f'lệch tuyến +{staged.total_detour_minutes} phút, phí VETC ~{staged.estimated_vetc_toll_vnd:,}đ.'
        )
        if live_drive > candidate.total_drive_minutes * 1.35:
            staged.warnings.append(f'Cảnh báo kẹt xe/sự cố trực tiếp từ Goong: Thời gian lái thực tế tăng lên {live_drive} phút (>1.35x chuẩn).')
        overview = route.get('overview_polyline')
        points = overview.get('points') if isinstance(overview, dict) else None
        if isinstance(points, str) and points.strip():
            # A waypoint-only provider route does not establish the selected alternative road.
            if staged.active_branch_id == 'MAIN_SPINE':
                staged.encoded_polyline = points.strip()
            else:
                staged.warnings.append('GOONG_BRANCH_GEOMETRY_UNVERIFIED: Giữ hình học nhánh cấu hình; nhà cung cấp chưa xác minh đúng nhánh.')
        for name in ('stops', 'total_drive_minutes', 'total_wait_minutes', 'total_duration_minutes',
                     'tradeoff_summary_vi', 'encoded_polyline', 'warnings'):
            setattr(candidate, name, getattr(staged, name))
        return candidate

    async def decorate_portfolio(
        self, origin: Coordinate, dest: Coordinate, candidates: List[ParetoCandidate],
        vehicle: str = 'car', departure_iso: str = '2026-10-16T06:00:00+07:00',
    ) -> List[ParetoCandidate]:
        if not self.api_key or not candidates:
            return candidates
        # Each child owns its bounded HTTP wait; the entire portfolio also has a wall-clock ceiling.
        tasks = [asyncio.create_task(self.decorate_candidate_polyline(origin, dest, c, vehicle, departure_iso)) for c in candidates]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=0.280)
        except asyncio.TimeoutError:
            results = [candidate if task.cancelled() else (task.exception() or task.result())
                       for task, candidate in zip(tasks, candidates)]
            for task, candidate in zip(tasks, candidates):
                if task.cancelled():
                    candidate.warnings.append('GOONG_UNAVAILABLE: Quá thời gian chờ trực tiếp; giữ ước tính ngoại tuyến.')
        feasible = []
        dropped = False
        for result in results:
            if isinstance(result, InfeasibleLiveSchedule):
                dropped = True
                continue
            if isinstance(result, BaseException):
                raise result
            feasible.append(result)
        candidates[:] = [c for c in feasible if not any(dominates(other, c) for other in feasible)]
        for candidate in candidates:
            if dropped:
                candidate.warnings.append('LIVE_CANDIDATE_DROPPED: Loại phương án vi phạm giờ mở cửa hoặc giới hạn UV sau khi cập nhật giao thông.')
            if len(candidates) < 3 and not any('PORTFOLIO_SHORTFALL' in w for w in candidate.warnings):
                candidate.warnings.append('PORTFOLIO_SHORTFALL: Không đủ 3 phương án không bị trội sau cập nhật trực tiếp.')
        return candidates
