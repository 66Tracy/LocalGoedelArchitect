import time
from kimina_client import KiminaClient

lean4_code = """import Mathlib
import Aesop

set_option maxHeartbeats 0

open BigOperators Real Nat Topology Rat

theorem imo1979_p1 (p q : ℤ) (hp : 0 < p) (hq : 0 < q)
    (h : (p : ℚ) / q = ∑ i ∈ Finset.range 1319, (-1 : ℚ)^i / (i + 1)) :
    1979 ∣ p := by sorry"""

client = KiminaClient("http://localhost:8000")
s_time = time.time()
res = client.check(lean4_code, reuse=False, timeout=600)
status = res.results[0].analyze().status.value # status = valid\lean_error\sorry\timeout_error
print(status)
print(f"耗时：{time.time()-s_time}")
print(res)