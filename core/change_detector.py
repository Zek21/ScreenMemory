"""
Screen change detection using perceptual hashing.
Detects meaningful visual changes between frames to avoid
processing unchanged or minimally-changed screens.
"""
import hashlib
import logging
from typing import Optional, Tuple
from dataclasses import dataclass
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ChangeResult:
    changed: bool
    hamming_distance: int
    change_percent: float
    current_hash: str
    previous_hash: Optional[str]


class ChangeDetector:
    """
    Detects meaningful screen changes using difference hash (dHash).
    dHash is fast, perceptually robust, and resilient to minor rendering
    differences (anti-aliasing, sub-pixel changes, cursor blink).
    """

    def __init__(self, hash_size: int = 16, threshold: int = 8, min_change_pct: float = 5.0):
        """
        Args:
            hash_size: Size of the hash grid (hash_size x hash_size produces hash_size*(hash_size-1) bits)
            threshold: Hamming distance threshold — changes below this are ignored
            min_change_pct: Minimum percentage of changed bits to trigger a capture
        """
        self.hash_size = hash_size
        self.threshold = threshold
        self.min_change_pct = min_change_pct
        self._previous_hashes: dict[int, str] = {}  # monitor_index -> hash
        self._hash_bits = hash_size * (hash_size - 1)

    def compute_dhash(self, image: Image.Image) -> str:
        """
        Compute difference hash (dHash) for an image.
        Compares adjacent pixels in a resized grayscale image.
        """
        # Resize to (hash_size+1, hash_size) and convert to grayscale
        resized = image.convert("L").resize(
            (self.hash_size + 1, self.hash_size), Image.Resampling.LANCZOS
        )
        pixels = np.array(resized, dtype=np.int16)

        # Compute horizontal gradient: each bit = 1 if pixel > right neighbor
        diff = pixels[:, 1:] > pixels[:, :-1]

        # Pack bits into hex string
        bits = diff.flatten()
        hash_int = 0
        for bit in bits:
            hash_int = (hash_int << 1) | int(bit)

        return format(hash_int, f"0{self._hash_bits // 4}x")

    def compute_phash(self, image: Image.Image) -> str:
        """
        Compute perceptual hash using DCT (more robust but slower than dHash).
        Used for secondary validation of large changes.
        """
        # Resize and grayscale
        size = self.hash_size
        img = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = np.array(img, dtype=np.float64)

        # Simple DCT approximation using numpy
        dct = np.fft.fft2(pixels).real
        # Use top-left quadrant (low frequencies)
        low_freq = dct[: size // 2, : size // 2]
        median = np.median(low_freq)
        bits = (low_freq > median).flatten()

        hash_int = 0
        for bit in bits:
            hash_int = (hash_int << 1) | int(bit)

        return format(hash_int, f"0{len(bits) // 4}x")

    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """Compute Hamming distance between two hex hash strings."""
        if len(hash1) != len(hash2):
            return self._hash_bits  # Max distance if incompatible

        val1 = int(hash1, 16)
        val2 = int(hash2, 16)
        xor = val1 ^ val2
        return bin(xor).count("1")

    def detect_change(self, image: Image.Image, monitor_index: int = 0) -> ChangeResult:
        """
        Detect if the screen has changed meaningfully since the last check.

        Returns a ChangeResult with changed=True if the visual difference
        exceeds the configured threshold.
        """
        current_hash = self.compute_dhash(image)
        previous_hash = self._previous_hashes.get(monitor_index)

        if previous_hash is None:
            # First capture — always counts as changed
            self._previous_hashes[monitor_index] = current_hash
            return ChangeResult(
                changed=True,
                hamming_distance=self._hash_bits,
                change_percent=100.0,
                current_hash=current_hash,
                previous_hash=None,
            )

        distance = self.hamming_distance(current_hash, previous_hash)
        change_pct = (distance / self._hash_bits) * 100

        changed = distance >= self.threshold and change_pct >= self.min_change_pct

        if changed:
            self._previous_hashes[monitor_index] = current_hash
            logger.debug(
                f"Monitor {monitor_index}: CHANGED (hamming={distance}, pct={change_pct:.1f}%)"
            )
        else:
            logger.debug(
                f"Monitor {monitor_index}: unchanged (hamming={distance}, pct={change_pct:.1f}%)"
            )

        return ChangeResult(
            changed=changed,
            hamming_distance=distance,
            change_percent=change_pct,
            current_hash=current_hash,
            previous_hash=previous_hash,
        )

    def force_reset(self, monitor_index: Optional[int] = None):
        """Reset stored hashes (next capture will always be 'changed')."""
        if monitor_index is not None:
            self._previous_hashes.pop(monitor_index, None)
        else:
            self._previous_hashes.clear()


class ContentRegionDetector:
    """
    Detects which regions of the screen have changed.
    Divides the screen into a grid and checks each cell independently.
    Useful for identifying which window/area changed without re-analyzing
    the entire screen.
    """

    def __init__(self, grid_rows: int = 4, grid_cols: int = 6, threshold: int = 4):
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols
        self.threshold = threshold
        self._previous_grid: dict[int, list] = {}

    def detect_changed_regions(
        self, image: Image.Image, monitor_index: int = 0
    ) -> list[Tuple[int, int, float]]:
        """
        Returns list of (row, col, change_percent) for regions that changed.
        """
        w, h = image.size
        cell_w = w // self.grid_cols
        cell_h = h // self.grid_rows

        detector = ChangeDetector(hash_size=8, threshold=self.threshold)
        current_hashes = []
        changed_regions = []

        for r in range(self.grid_rows):
            row_hashes = []
            for c in range(self.grid_cols):
                x1 = c * cell_w
                y1 = r * cell_h
                x2 = min(x1 + cell_w, w)
                y2 = min(y1 + cell_h, h)

                cell = image.crop((x1, y1, x2, y2))
                cell_hash = detector.compute_dhash(cell)
                row_hashes.append(cell_hash)

                prev_grid = self._previous_grid.get(monitor_index)
                if prev_grid and r < len(prev_grid) and c < len(prev_grid[r]):
                    dist = detector.hamming_distance(cell_hash, prev_grid[r][c])
                    pct = (dist / detector._hash_bits) * 100
                    if dist >= self.threshold:
                        changed_regions.append((r, c, pct))
                else:
                    changed_regions.append((r, c, 100.0))

            current_hashes.append(row_hashes)

        self._previous_grid[monitor_index] = current_hashes
        return changed_regions


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    from PIL import ImageGrab

    detector = ChangeDetector(hash_size=16, threshold=8)
    region_detector = ContentRegionDetector()

    print("Capturing two frames 2 seconds apart...")
    img1 = ImageGrab.grab()
    result1 = detector.detect_change(img1, 0)
    print(f"Frame 1: changed={result1.changed} (first frame always true)")

    import time
    time.sleep(2)

    img2 = ImageGrab.grab()
    result2 = detector.detect_change(img2, 0)
    print(f"Frame 2: changed={result2.changed}, distance={result2.hamming_distance}, pct={result2.change_percent:.1f}%")

    regions = region_detector.detect_changed_regions(img2, 0)
    print(f"Changed regions: {len(regions)} of {region_detector.grid_rows * region_detector.grid_cols}")
