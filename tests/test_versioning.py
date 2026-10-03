import sys
sys.path.insert(0,'backend')
from app.versioning import identify_version
for fn,parent,rank,topic in [
 ('guide_v1.docx','',1,'guide'),
 ('guide_v3.docx','',3,'guide'),
 ('manual.pdf','topic/v4',4,'topic/manual'),
 ('manual.pdf','topic/version-12',12,'topic/manual'),
 ('manual.pdf','topic/rev3',3,'topic/manual')]:
    x=identify_version(fn,parent); assert x.version_rank==rank,(x,rank); assert x.topic_key==topic,(x,topic)
print('versioning tests OK')
