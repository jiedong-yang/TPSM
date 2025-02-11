import matplotlib
matplotlib.use('Agg')
import os
import sys
import yaml
from argparse import ArgumentParser
from tqdm import tqdm
from scipy.spatial import ConvexHull
import numpy as np
# import imageio
import imageio.v2 as imageio
from skimage.transform import resize
from skimage import img_as_ubyte
import torch
from modules.inpainting_network import InpaintingNetwork
from modules.keypoint_detector import KPDetector
from modules.dense_motion import DenseMotionNetwork
from modules.avd_network import AVDNetwork

from typing import List
from functions import crop_face, replace, get_fa_kps, save_images
import face_alignment

import gc

from concurrent.futures import ProcessPoolExecutor

gc.enable()

if sys.version_info[0] < 3:
    raise Exception("You must use Python 3 or higher. Recommended version is Python 3.9")


def relative_kp(kp_source, kp_driving, kp_driving_initial):

    source_area = ConvexHull(kp_source['fg_kp'][0].data.cpu().numpy()).volume
    driving_area = ConvexHull(kp_driving_initial['fg_kp'][0].data.cpu().numpy()).volume
    adapt_movement_scale = np.sqrt(source_area) / np.sqrt(driving_area)

    kp_new = {k: v for k, v in kp_driving.items()}

    kp_value_diff = (kp_driving['fg_kp'] - kp_driving_initial['fg_kp'])
    kp_value_diff *= adapt_movement_scale
    kp_new['fg_kp'] = kp_value_diff + kp_source['fg_kp']

    return kp_new


def load_checkpoints(config_path, checkpoint_path, device):
    with open(config_path) as f:
        config = yaml.full_load(f)

    inpainting = InpaintingNetwork(**config['model_params']['generator_params'],
                                   **config['model_params']['common_params'])
    kp_detector = KPDetector(**config['model_params']['common_params'])
    dense_motion_network = DenseMotionNetwork(**config['model_params']['common_params'],
                                              **config['model_params']['dense_motion_params'])
    avd_network = AVDNetwork(num_tps=config['model_params']['common_params']['num_tps'],
                             **config['model_params']['avd_network_params'])
    kp_detector.to(device)
    dense_motion_network.to(device)
    inpainting.to(device)
    avd_network.to(device)
       
    checkpoint = torch.load(checkpoint_path, map_location=device)
 
    inpainting.load_state_dict(checkpoint['inpainting_network'])
    kp_detector.load_state_dict(checkpoint['kp_detector'])
    dense_motion_network.load_state_dict(checkpoint['dense_motion_network'])
    if 'avd_network' in checkpoint:
        avd_network.load_state_dict(checkpoint['avd_network'])
    
    inpainting.eval()
    kp_detector.eval()
    dense_motion_network.eval()
    avd_network.eval()
    
    return inpainting, kp_detector, dense_motion_network, avd_network


def make_animation(source_image, driving_video, inpainting_network, kp_detector, dense_motion_network, avd_network, device, mode='relative'):
    assert mode in ['standard', 'relative', 'avd']
    with torch.no_grad():
        predictions = []
        source = torch.tensor(source_image[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2)
        source = source.to(device)
        driving = torch.tensor(np.array(driving_video)[np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3).to(device)
        kp_source = kp_detector(source)
        kp_driving_initial = kp_detector(driving[:, :, 0])

        for frame_idx in tqdm(range(driving.shape[2])):
            driving_frame = driving[:, :, frame_idx]
            driving_frame = driving_frame.to(device)
            kp_driving = kp_detector(driving_frame)
            if mode == 'standard':
                kp_norm = kp_driving
            elif mode == 'relative':
                kp_norm = relative_kp(kp_source=kp_source, kp_driving=kp_driving,
                                      kp_driving_initial=kp_driving_initial)
            elif mode == 'avd':
                kp_norm = avd_network(kp_source, kp_driving)
            dense_motion = dense_motion_network(source_image=source, kp_driving=kp_norm,
                                                kp_source=kp_source, bg_param=None,
                                                dropout_flag=False)
            out = inpainting_network(source, dense_motion)

            predictions.append(np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0])
    return predictions


def find_best_frame(source, driving, cpu):
    import face_alignment

    def normalize_kp(kp):
        kp = kp - kp.mean(axis=0, keepdims=True)
        area = ConvexHull(kp[:, :2]).volume
        area = np.sqrt(area)
        kp[:, :2] = kp[:, :2] / area
        return kp

    fa = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D, flip_input=True,
                                      device='cpu' if cpu else 'cuda')
    kp_source = fa.get_landmarks(255 * source)[0]
    kp_source = normalize_kp(kp_source)
    norm = float('inf')
    frame_num = 0
    for i, image in tqdm(enumerate(driving)):
        try:
            kp_driving = fa.get_landmarks(255 * image)[0]
            kp_driving = normalize_kp(kp_driving)
            new_norm = (np.abs(kp_source - kp_driving) ** 2).sum()
            if new_norm < norm:
                norm = new_norm
                frame_num = i
        except TypeError:
            pass
    return frame_num


def load_video(video, img_shape):
    reader = imageio.get_reader(video)
    fps = reader.get_meta_data()['fps']
    video = []
    try:
        for im in reader:
            video.append(im)
    except RuntimeError:
        pass
    reader.close()

    return [resize(frame, img_shape)[..., :3] for frame in video], fps


def inference(
        inpainting,
        kp_detector,
        dense_motion_network,
        avd_network,
        source_image: str,
        driving_video: List[np.ndarray],
        result_video,
        img_shape,
        fps,
        mode,
        is_find_best_frame=False,
        cpu=False,
        save_as_frames=False,
        selected_frames: List[int] = None,
        crop_replace=False,
        crop_size=256,
        n_workers=8
):
    """ inference on single image

    :param inpainting:
    :param kp_detector:
    :param dense_motion_network:
    :param avd_network:
    :param source_image:
    :param driving_video:
    :param result_video:
    :param img_shape:
    :param fps:
    :param mode:
    :param is_find_best_frame:
    :param cpu:
    :param save_as_frames:
    :param selected_frames:
    :param crop_replace:
    :param crop_size:
    :param n_workers:
    :return:
    """
    if cpu:
        device = torch.device('cpu')
    else:
        device = torch.device('cuda')

    result_dir = os.path.dirname(result_video)
    os.makedirs(result_dir, exist_ok=True)

    original_image = imageio.imread(source_image)

    if crop_replace:
        print("cropping images based on facial key points")
        fa = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D, flip_input=True, device="cuda")
        fa_id = [27, 30, 57, 8, 0, 16]
        fa_kps = get_fa_kps(original_image, fa)[fa_id, :]

        # crop
        x, y = int((fa_kps[4, 0]+fa_kps[5, 0])/2), int((fa_kps[0,1]+fa_kps[1,1])/2)
        # x, y = int(fa_kps[1, 0]), int(fa_kps[1, 1])
        # TODO: update the off_x, off_y parameters to automatic configurations
        crop_image_name = f"{os.path.splitext(os.path.basename(source_image))[0]}_cropped_({x}-{y}).png"
        source_image, top_left = crop_face(original_image, (x, y),
                                           off_x=crop_size//2, off_y=crop_size//2, size=crop_size)
        imageio.imsave(os.path.join(result_dir, crop_image_name), source_image)
        source_image = resize(source_image, img_shape)[..., :3]
    else:
        source_image = resize(original_image, img_shape)[..., :3]

    if is_find_best_frame:
        i = find_best_frame(source_image, driving_video, cpu)
        print("Best frame: " + str(i))
        driving_forward = driving_video[i:]
        driving_backward = driving_video[:(i + 1)][::-1]
        predictions_forward = make_animation(source_image, driving_forward, inpainting, kp_detector,
                                             dense_motion_network, avd_network, device=device, mode=mode)
        predictions_backward = make_animation(source_image, driving_backward, inpainting, kp_detector,
                                              dense_motion_network, avd_network, device=device, mode=mode)
        predictions = predictions_backward[::-1] + predictions_forward[1:]
    else:
        predictions = make_animation(source_image, driving_video, inpainting, kp_detector,
                                     dense_motion_network, avd_network, device=device, mode=mode)

    frames = [img_as_ubyte(frame) for frame in predictions]

    if crop_replace:
        frames = [replace(original_image, repl_img=frame, top_left_point=top_left, size=crop_size) for frame in frames]

    imageio.mimsave(result_video, frames, fps=fps)
    if save_as_frames:
        postfix = '-frames' if not crop_replace else '-cr-frames'
        frame_dir = os.path.join(
            result_dir,
            os.path.splitext(os.path.basename(result_video))[0] + postfix
        )
        os.makedirs(frame_dir, exist_ok=True)

        if selected_frames:
            for idx in selected_frames:
                imageio.imsave(os.path.join(frame_dir, f"{str(idx).zfill(3)}.png"), frames[idx])
            return

        # for i, im in tqdm(enumerate(frames)):
        #     imageio.imsave(os.path.join(frame_dir, f"{str(i).zfill(3)}.png"), im)
        filepath_list = [os.path.join(frame_dir, f"{str(i).zfill(3)}.png") for i in range(len(frames))]
        # n_workers = 8
        chunksize = round(len(filepath_list) / n_workers)
        with ProcessPoolExecutor(n_workers) as exe:
            for i in tqdm(range(0, len(filepath_list), chunksize)):
                # fp_list = filepath_list[i: (i+chunksize)]
                _ = exe.submit(save_images, frames[i: i+chunksize], filepath_list[i: (i+chunksize)])

    del predictions

    gc.collect()


def inference_func(args):
    # load computation module
    inpainting, kp_detector, dense_motion_network, avd_network = load_checkpoints(
        config_path=args.config, checkpoint_path=args.checkpoint,
        device=torch.device('cpu') if args.cpu else torch.device('cuda')
    )

    # load driving video
    driving_video, fps = load_video(args.driving_video, img_shape=args.img_shape)

    if args.image_dir and os.path.isdir(args.image_dir):
        images = sorted(os.listdir(args.image_dir))
        # init result directory
        result_dir = args.result_dir if args.result_dir else './results'
        os.makedirs(result_dir, exist_ok=True)

        for image in tqdm(images):
            # get driving video filename
            driving_vid_name = os.path.splitext(os.path.basename(args.driving_video))[0]
            # get image filename
            image_name = os.path.splitext(image)[0]
            # init result video's name for each image
            result_vid_name = '-'.join([image_name, driving_vid_name, args.mode]) + ".mp4"

            # inference
            inference(
                inpainting=inpainting,
                kp_detector=kp_detector,
                dense_motion_network=dense_motion_network,
                avd_network=avd_network,
                source_image=os.path.join(args.image_dir, image),
                driving_video=driving_video,
                result_video=os.path.join(result_dir, result_vid_name),
                img_shape=args.img_shape,
                fps=fps,
                mode=args.mode,
                is_find_best_frame=args.find_best_frame,
                cpu=args.cpu,
                save_as_frames=args.save_as_frames,
                selected_frames=args.selected_frames,
                crop_replace=args.crop_replace,
                crop_size=args.crop_size,
                n_workers=args.n_workers,
            )
    else:
        # single source image inference
        inference(
            inpainting=inpainting,
            kp_detector=kp_detector,
            dense_motion_network=dense_motion_network,
            avd_network=avd_network,
            source_image=args.source_image,
            driving_video=driving_video,
            result_video=args.result_video,
            img_shape=args.img_shape,
            fps=fps,
            mode=args.mode,
            is_find_best_frame=args.find_best_frame,
            cpu=args.cpu,
            save_as_frames=args.save_as_frames,
            selected_frames=args.selected_frames,
            crop_replace=args.crop_replace,
            crop_size=args.crop_size,
            n_workers=args.n_workers,
        )


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--config", default='config/vox-256.yaml', help="path to config")
    parser.add_argument("--checkpoint", default='checkpoints/vox.pth.tar', help="path to checkpoint to restore")

    parser.add_argument("--source_image", default='./assets/source.png', nargs='+', help="path to source image/images")
    parser.add_argument("--driving_video", default='./assets/driving.mp4', help="path to driving video")
    parser.add_argument("--result_video", default='./result.mp4', help="path to output")
    parser.add_argument("--image_dir", help="directory contains multiple source images")
    parser.add_argument("-rd", "--result_dir",
                        help="default output directory if doing multiple images inference")
    
    parser.add_argument("--img_shape", default="256,256", type=lambda x: list(map(int, x.split(','))),
                        help='Shape of image, that the model was trained on.')
    
    parser.add_argument("--mode", default='relative', choices=['standard', 'relative', 'avd'],
                        help="Animate mode: ['standard', 'relative', 'avd'], when use the relative mode to animate "
                             "a face, use '--find_best_frame' can get better quality result")
    
    parser.add_argument("--find_best_frame", dest="find_best_frame", action="store_true", 
                        help="Generate from the frame that is the most alligned with source. "
                             "(Only for faces, requires face_aligment lib)")

    parser.add_argument("--cpu", dest="cpu", action="store_true", help="cpu mode.")

    parser.add_argument("-saf", "--save_as_frames", action="store_true", help="same frames instead of video")
    parser.add_argument("-sf", "--selected_frames", nargs='+', type=lambda x: list(map(int, x)),
                        help="a list of frame index of the frames to save as image")

    parser.add_argument('-cr', "--crop_replace", action="store_true", help="crop and replace method")
    parser.add_argument('-cs', "--crop_size", default=256, type=int, help="size of cropped out image")
    parser.add_argument('-nw', "--n_workers", default=8, type=int, help="number of processes for save images")

    opt = parser.parse_args()

    torch.cuda.empty_cache()

    inference_func(opt)


