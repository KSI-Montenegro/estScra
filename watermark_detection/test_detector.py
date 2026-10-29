import unittest
import numpy as np
import cv2
import detect_watermarks as d


def background(seed):
    rng=np.random.default_rng(seed)
    im=np.full((480,640,3),100,dtype=np.uint8)
    for _ in range(25):
        x,y=map(int,rng.integers([10,10],[630,470]));radius=int(rng.integers(10,70));color=tuple(map(int,rng.integers(20,235,3)))
        cv2.circle(im,(x,y),radius,color,-1)
    return cv2.GaussianBlur(im,(9,9),2)

def logo():
    im=np.full((44,170,3),245,dtype=np.uint8)
    cv2.putText(im,'GRADEN',(5,31),cv2.FONT_HERSHEY_SIMPLEX,0.85,(15,15,15),2,cv2.LINE_AA)
    cv2.rectangle(im,(3,3),(164,40),(15,15,15),1)
    return im

def watermarked(seed,scale=1):
    im=background(seed);mark=logo()
    if scale!=1:mark=cv2.resize(mark,None,fx=scale,fy=scale)
    h,w=mark.shape[:2];im[480-h-15:465,640-w-15:625]=mark
    return im

class DetectorTests(unittest.TestCase):
    def test_template_finds_known_logo_and_location(self):
        matches=d.template_matches(watermarked(1),[('known',logo())])
        self.assertEqual(len(matches),1)
        x1,y1,x2,y2=matches[0]['box_original_pixels']
        self.assertLessEqual(abs(x1-455),2);self.assertLessEqual(abs(y1-421),2)
        self.assertGreaterEqual(matches[0]['score'],0.72)
    def test_template_multiscale(self):
        self.assertTrue(d.template_matches(watermarked(2,1.25),[('known',logo())]))
    def test_unwatermarked_fixture_does_not_match(self):
        self.assertEqual(d.template_matches(background(1),[('known',logo())]),[])
    def test_blank_template_is_not_a_match(self):
        self.assertEqual(d.template_matches(background(1),[('blank',np.zeros((40,100,3),np.uint8))]),[])
    def test_repeated_overlay_uses_distinct_photos_and_localizes(self):
        det=d.RepeatDetector(min_properties=6)
        for seed in range(8):
            _,e,h,ratio=d.prepare(watermarked(seed))
            det.add('test',ratio,'property_'+str(seed),h,e)
        det.build();_,positive,h,ratio=d.prepare(watermarked(30))
        matches=det.score('test',ratio,positive)
        self.assertTrue(any(m['region']=='bottom_right' for m in matches))
        _,negative,_,_=d.prepare(background(30))
        self.assertFalse(any(m['region']=='bottom_right' for m in det.score('test',ratio,negative)))
    def test_one_property_cannot_establish_repeat_support(self):
        det=d.RepeatDetector(min_properties=6)
        for seed in range(8):
            _,e,h,ratio=d.prepare(watermarked(seed));det.add('test',ratio,'same_property',h,e)
        self.assertEqual(det.build(),{})
    def test_duplicate_photos_cannot_establish_repeat_support(self):
        det=d.RepeatDetector(min_properties=6);_,e,h,ratio=d.prepare(watermarked(2))
        for seed in range(8):det.add('test',ratio,'property_'+str(seed),h,e)
        self.assertEqual(det.build(),{})
    def test_balanced_sample_uses_different_properties(self):
        rows=[{'source':s,'property_key':s+str(k),'image_number':n} for s in ('a','b','c') for k in range(5) for n in (1,2)]
        sample=d.sample_records(rows,9)
        self.assertEqual(len(sample),9)
        self.assertEqual(len({r['property_key'] for r in sample}),9)
        self.assertEqual([sum(r['source']==s for r in sample) for s in ('a','b','c')],[3,3,3])

if __name__=='__main__':unittest.main()
