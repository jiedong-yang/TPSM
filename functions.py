import os

import numpy as np


def crop(image: np.ndarray, center, off_x=200, off_y=250, size=400):

    x, y = center
    print(x, y)
    bottom_left = (x-off_x, y-off_y)
    print(f"bottom_left: {bottom_left}")
    return image[bottom_left[1]: bottom_left[1]+size, bottom_left[0]: bottom_left[0]+size, :], bottom_left


def replace(image: np.ndarray, repl_img: np.ndarray, bl_point: tuple, size=400):
    """

    :param image: original image
    :param repl_img:
    :param bl_point:
    :param size:
    :return:
    """
    new_image = image.copy() # deep copy
    new_image[bl_point[1]: bl_point[1]+size, bl_point[0]: bl_point[0]+size, :] = repl_img
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