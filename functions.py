import os
import cv2
import numpy as np


def crop(image: np.ndarray, center, off_x=128, off_y=128, size=256):
    def adjust_coord(n, max_n):
        return min(max(n, 0), max_n)

    img_size = image.shape[0]
    x, y = center
    print(x, y)
    top_left = (adjust_coord(x-off_x), adjust_coord(y-off_y)) #
    print(f"top_left: {top_left}")
    return image[top_left[1]: top_left[1]+size, top_left[0]: top_left[0]+size, :3], top_left


def replace(image: np.ndarray, repl_img: np.ndarray, top_left_point: tuple, size=256):
    """

    :param image: original image
    :param repl_img:
    :param top_left_point:
    :param size:
    :return:
    """
    new_image = image.copy() # deep copy
    new_image[top_left_point[1]: top_left_point[1]+size, top_left_point[0]: top_left_point[0]+size, :] = repl_img
    return new_image


def frames_to_video(img_dir: str, output='output.mp4', img_format='png', video_format='mp4', fps=30):
    import imageio.v2 as iio

    name, ext = os.path.splitext(output)
    ext = '.'+video_format
    output = name + ext

    files = [os.path.join(img_dir, file) for file in sorted(os.listdir(img_dir)) if file.endswith(img_format)]
    writer = iio.get_writer(output, fps=fps)

    for im in files:
        writer.append_data(iio.imread(im))
    writer.close()


def get_fa_kps(img, fa):
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    kps = fa.get_landmarks(img)
    if kps is None:
        return None
    return kps[0]


# def need_crop(image):
#