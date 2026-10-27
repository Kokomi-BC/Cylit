
from PIL import Image
im = Image.open("data/logs/cdp_hr2.png")
print(im.size)
crop = im.crop((860, 580, 1374, 720)).resize((514*2, 140*2), Image.LANCZOS)
crop.save("data/logs/crop_hr.png")
print("saved")
