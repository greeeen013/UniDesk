
import sys
import os

# Add the project root to sys.path to allow importing from 'unidesk'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from unidesk.common.config import MonitorRect, VirtualPlacement
from unidesk.server.edge_detector import compute_virtual_rect, EdgeDetector

def test_scaling():
    print("Testing scaling logic...")
    
    # Server monitor: 1000x800
    server_monitors = [
        MonitorRect(id=0, left=0, top=0, right=1000, bottom=800, is_primary=True)
    ]
    
    # Client monitor: 500x400
    client_monitor = MonitorRect(id=1, left=0, top=0, right=500, bottom=400)
    
    placement = VirtualPlacement(
        client_id="client1",
        anchor_monitor_id=0,
        anchor_edge="bottom",
        offset_pixels=0
    )
    
    print("\n--- CASE 1: scale_to_snap = False ---")
    rect_normal = compute_virtual_rect(placement, server_monitors, client_monitor, scale_to_snap=False)
    print(f"Virtual Rect: {rect_normal}")
    # Should be (0, 800) - (500, 1200)
    assert rect_normal.left == 0
    assert rect_normal.right == 500
    assert rect_normal.top == 800
    assert rect_normal.bottom == 1200
    
    print("\n--- CASE 2: scale_to_snap = True ---")
    rect_scaled = compute_virtual_rect(placement, server_monitors, client_monitor, scale_to_snap=True)
    print(f"Virtual Rect (scaled): {rect_scaled}")
    # Should be (0, 800) - (1000, 1200) -> Width matches server
    assert rect_scaled.left == 0
    assert rect_scaled.right == 1000
    assert rect_scaled.top == 800
    assert rect_scaled.bottom == 1200
    
    # Test translation
    edge = EdgeDetector(server_monitors, scale_to_snap=True)
    edge.update_placement(placement, client_monitor)
    
    # Test point at middle of server (500, 800)
    cid, cx, cy = edge.hit_test(500, 800)
    print(f"Hit test at (500, 800): {cid}, ({cx}, {cy})")
    # 500 in a 1000 wide zone should be 250 in a 500 wide monitor
    assert cx == 250
    
    # Test point at far right (1000, 800)
    # Note: hit_test uses contains which is [left, right)
    cid, cx, cy = edge.hit_test(999, 800)
    print(f"Hit test at (999, 800): {cid}, ({cx}, {cy})")
    # 999 in 1000 should be ~499 in 500
    assert cx == int(999/1000 * 500)
    
    print("\nAll tests passed!")

if __name__ == "__main__":
    try:
        test_scaling()
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)
