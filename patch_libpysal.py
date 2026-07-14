#!/usr/bin/env python3
"""
Patch libpysal weights.py to fix the cardinalities property.

This script ensures the W.cardinalities property correctly initializes
neighbors for all observations in _id_order, even those with no neighbors.
"""

import re
import sys
from pathlib import Path


def find_libpysal_weights():
    """Locate libpysal weights.py in the virtual environment."""
    import libpysal
    libpysal_path = Path(libpysal.__file__).parent
    weights_path = libpysal_path / "weights" / "weights.py"
    return weights_path


def get_patched_cardinalities():
    """Return the patched cardinalities property method."""
    return '''@property
    def cardinalities(self):
        """Number of neighbors for each observation."""
        if "cardinalities" not in self._cache:
            c = {}
            for i in self._id_order:
                if i in self.neighbors:
                    c[i] = len(self.neighbors[i])
                else:
                    self.neighbors[i] = []
                    c[i] = 0
            self._cardinalities = c
            self._cache["cardinalities"] = self._cardinalities
        return self._cardinalities'''


def check_patch_applied(content):
    """Check if the patch is already applied."""
    patched = get_patched_cardinalities()
    return patched in content


def apply_patch(weights_file):
    """Apply the cardinalities patch to weights.py."""
    with open(weights_file, "r") as f:
        content = f.read()
    
    if check_patch_applied(content):
        print(f"✓ Patch already applied to {weights_file}")
        return True
    
    # Find and replace the cardinalities property
    # Pattern matches the property definition and its body
    pattern = r'@property\s+def cardinalities\(self\):.*?return self\._cardinalities'
    
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print(f"✗ Could not find cardinalities property in {weights_file}")
        return False
    
    # Replace with patched version
    patched = get_patched_cardinalities()
    new_content = content[:match.start()] + patched + content[match.end():]
    
    # Write back
    with open(weights_file, "w") as f:
        f.write(new_content)
    
    print(f"✓ Patch applied to {weights_file}")
    
    # Verify
    with open(weights_file, "r") as f:
        verify_content = f.read()
    
    if check_patch_applied(verify_content):
        print("✓ Patch verified successfully")
        return True
    else:
        print("✗ Patch verification failed")
        return False


def main():
    try:
        weights_file = find_libpysal_weights()
    except Exception as e:
        print(f"✗ Error locating libpysal: {e}")
        sys.exit(1)
    
    if not weights_file.exists():
        print(f"✗ File not found: {weights_file}")
        sys.exit(1)
    
    print(f"Patching {weights_file}...")
    success = apply_patch(weights_file)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
