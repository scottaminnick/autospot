#!/usr/bin/env python3
"""Read-only snapshot geometry check; not the production geospatial validator.

Dateline regression cases are local planar checks on unwrapped rings.
Polar/shared-edge operational routing still requires the PR 3 implementation.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/phase0/domain"

def unwrap(r):
 out=[r[0]]
 for x,y in r[1:]:
  while x-out[-1][0]>180:x-=360
  while x-out[-1][0]<-180:x+=360
  out.append([x,y])
 return out

def relation(r,x,y):
 inside=False
 for (a,b),(c,d) in zip(r,r[1:]):
  cross=(x-a)*(d-b)-(y-b)*(c-a)
  if abs(cross)<1e-8 and min(a,c)<=x<=max(a,c) and min(b,d)<=y<=max(b,d):return 'boundary'
  if (b>y)!=(d>y) and x<(c-a)*(y-b)/(d-b)+a:inside=not inside
 return 'inside' if inside else 'outside'
def regions(features,lat,lon):
 found=[]
 for ft in features:
  for poly in ft['geometry']['coordinates']:
   outer=unwrap(poly[0]);center=sum(c[0] for c in outer)/len(outer)
   x=lon+360*round((center-lon)/360)
   r=relation(outer,x,lat)
   if r!='outside' and not any(relation(unwrap(h),x,lat)=='inside' for h in poly[1:]):found.append((ft['properties'].get('name','AWC'),r))
 return found


def main():
    awc = json.loads((ROOT / "awc_sigmet_area.json").read_text())["features"]
    alaska = json.loads((ROOT / "aawu-firs.geojson").read_text())["features"]
    cases = json.loads((ROOT / "test-locations.json").read_text())["locations"]
    for case in cases:
        a = regions(awc, case["latitude"], case["longitude"])
        b = regions(alaska, case["latitude"], case["longitude"])
        actual = ("boundary_review" if any(x[1] == "boundary" for x in a + b)
                  else "conflict_review" if a and b else "AWC" if a
                  else "AAWU" if b else "other_office_review")
        assert actual == case["expected_routing"], (case["id"], actual)
        assert sorted(x[0] for x in b) == sorted(case["aawu_firs"])
    print(f"Verified {len(cases)} source-geometry reference locations; operational scope not asserted.")


if __name__ == "__main__":
    main()
