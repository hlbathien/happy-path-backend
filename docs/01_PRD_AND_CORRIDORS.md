# 01 — Product Requirements & Physical Corridor Topologies (Freeze-Spec v3.0)

**Corridor Topologies:** The topology intervals below define the physical stop-exclusion contracts for highway interchanges. Toll pricing sums registered, branch-compatible plazas inside the requested slice. Supplied tariff and road parameters remain operational estimates pending live verification.

## 1. Product Vision & Real-World Peace-of-Mind Standard
Happy Path synthesizes **3 Pareto-optimal road-trip itineraries** (`P1_EXPRESS`, `P2_ICONIC`, `P3_SCENIC`) across 3 bidirectional Southern Vietnam corridors. A route is only acceptable if a real family or driver in Vietnam can execute it at the requested hour without encountering closed venues, impossible cross-mountain highway jumps, illegal median-barrier U-turns, midday heat exhaustion, EV battery stranding, or corrupted voice-session state.

## 2. Physical Highway Interchange Topology (`corridor_branches`)

### 2.1 Corridor 1: HCMC <-> Da Lat (`hcm_dalat`, 308.0 km)
- **`MAIN_SPINE`** (`[0.0, 308.0] km`, Base VETC Toll: `136,000 VND` covering CT01 HCMC–Dầu Giây `98k` + Liên Khương `38k`):
  - Follows `CT01 -> Dầu Giây (Km 55) -> QL20 Định Quán (Km 114) -> Đèo Bảo Lộc -> Bảo Lộc (Km 190) -> Di Linh -> Đức Trọng (Km 260) -> Đèo Prenn -> Đà Lạt (Km 308)`.
- **`BRANCH_BAOLOC_LOOP`** (`diverge_km = 175.0`, `merge_km = 212.0`, Toll Delta: `+0 VND`):
  - Diverges from `QL20` at Bảo Lộc (`Km 175`) into the Dambri / Tâm Châu highland loop and rejoins `QL20` at `Km 212`.
- **`BRANCH_QL28B_DAININH`** (`diverge_km = 55.0`, `merge_km = 262.0`, Toll Delta: `+125,000 VND`, Total Toll: `261,000 VND`):
  - Instead of turning north onto `QL20` at Dầu Giây (`Km 55`), the vehicle stays on the **CT01 Dầu Giây–Phan Thiết–Vĩnh Hảo Expressway** (`+125,000 VND` additional VETC toll) to the Lương Sơn interchange, climbs `QL28B Đèo Đại Ninh`, and rejoins `QL20` at Ninh Gia / Đức Trọng (`Km 262`).
  - **Physical Interchange Law**: A vehicle on `BRANCH_QL28B_DAININH` is physically on `CT01/QL28B` between `Km 55.0` and `Km 262.0` and **cannot** visit `MAIN_SPINE` (`QL20`) stops located inside `(55.0, 262.0)` (such as Định Quán `Km 114` or Bảo Lộc `Km 190`)! It CAN visit `MAIN_SPINE` stops upstream of `Km 55.0` (e.g., Long Thành EV Rest Stop at `Km 42.0`) or downstream of `Km 262.0` (e.g., Liên Khương / Prenn / Đà Lạt at `Km 278–300`).

### 2.2 Corridor 2: HCMC <-> Nha Trang (`hcm_nhatrang`, 435.0 km)
- **`MAIN_SPINE`** (`[0.0, 435.0] km`, Base VETC Toll: `309,000 VND` covering full CT01 HCMC–Dầu Giây–Phan Thiết–Vĩnh Hảo–Cam Lâm):
  - High-speed expressway spine with EV fast-charging rest stops at Long Thành (`Km 42`), Hàm Tân (`Km 135`), and Tuy Phong / Cà Ná (`Km 310`).
- **`BRANCH_VINHHY_DT702`** (`diverge_km = 325.0`, `merge_km = 395.0`, Toll Delta: `-25,000 VND`, Total Toll: `284,000 VND`):
  - Diverges from `CT01/QL1A` at Phan Rang (`Km 325`) onto coastal `DT702` through Ninh Chữ, Hang Rái, and Vĩnh Hy Bay before rejoining at Cam Ranh (`Km 395`).
- **`BRANCH_BAUTRANG_COAST`** (`diverge_km = 180.0`, `merge_km = 395.0`, Toll Delta: `-65,000 VND`, Total Toll: `244,000 VND`):
  - Exits `CT01` early at Ba Bàu / Phan Thiết (`Km 180`), saving `-65,000 VND` in downstream expressway tolls, and follows the iconic coastal road (`DT716 Bàu Trắng -> Mũi Dinh -> DT702 Vĩnh Hy`) all the way to Cam Ranh (`Km 395`). Between `Km 180` and `Km 395`, it cannot visit inland `MAIN_SPINE` stops.

### 2.3 Corridor 3: HCMC <-> Vung Tau (`hcm_vungtau`, 115.0 km)
- **`MAIN_SPINE`** (`[0.0, 115.0] km`, Base VETC Toll: `81,000 VND` covering CT01 Long Thành + QL51 BOT):
  - Divided highway `QL51` with strict median barriers (`OUTBOUND`-only eastbound stops vs `INBOUND`-only westbound return stops).
- **`BRANCH_LONGSON_ISLAND`** (`diverge_km = 62.0`, `merge_km = 96.0`, Toll Delta: `-15,000 VND`, Total Toll: `66,000 VND`):
  - Diverges at Phú Mỹ / Tân Hải (`Km 62`) across Long Sơn Island (Nhà Lớn Long Sơn & oyster rafts) and rejoins via Gò Găng bridge into Vũng Tàu (`Km 96`).
- **`BRANCH_SUOINGHE_LOOP`** (`diverge_km = 46.0`, `merge_km = 96.0`, Toll Delta: `-15,000 VND`, Total Toll: `66,000 VND`):
  - Diverges at Mỹ Xuân / Ngãi Giao (`Km 46`) through Đồi Cừu Suối Nghệ and Bà Rịa rural loop, rejoining at `Km 96`.
