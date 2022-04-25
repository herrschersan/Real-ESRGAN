import argparse
import cv2
import glob
import mimetypes
import numpy as np
import os
import queue
import shutil
import torch
from basicsr.archs.rrdbnet_arch import RRDBNet
from basicsr.utils.logger import AvgTimer
from tqdm import tqdm

from realesrgan import IOConsumer, PrefetchReader, RealESRGANer
from realesrgan.archs.srvgg_arch import SRVGGNetCompact


def get_frames(args, extract_frames=False):
    # input can be a video file / a folder of frames / an image
    is_video = False
    if mimetypes.guess_type(args.input)[0].startswith('video'):  # is a video file
        is_video = True
        video_name = os.path.splitext(os.path.basename(args.input))[0]
        if extract_frames:
            frame_folder = os.path.join('tmp_frames', video_name)
            os.makedirs(frame_folder, exist_ok=True)
            # use ffmpeg to extract frames
            os.system(f'ffmpeg -i {args.input} -qscale:v 1 -qmin 1 -qmax 1 -vsync 0  {frame_folder}/frame%08d.png')
            # get image path list
            paths = sorted(glob.glob(os.path.join(frame_folder, '*')))
        else:
            paths = []
        # get input video fps
        if args.fps is None:
            import ffmpeg
            probe = ffmpeg.probe(args.input)
            video_streams = [stream for stream in probe['streams'] if stream['codec_type'] == 'video']
            args.fps = eval(video_streams[0]['avg_frame_rate'])
    elif mimetypes.guess_type(args.input)[0].startswith('image'):  # is an image file
        paths = [args.input]
    else:
        paths = sorted(glob.glob(os.path.join(args.input, '*')))
        assert len(paths) > 0, 'the input folder is empty'

    if args.fps is None:
        args.fps = 24

    return is_video, paths


def inference_stream(args, upsampler, face_enhancer):
    try:
        import ffmpeg
    except ImportError as e:
        print("please install ffmpeg-python package! The command line may be: pip3 install ffmpeg-python")
        raise e

    is_video, paths = get_frames(args, extract_frames=False)
    video_name = os.path.splitext(os.path.basename(args.input))[0]
    video_save_path = os.path.join(args.output, f'{video_name}_{args.suffix}.mp4')

    # decoder
    if is_video:
        # get height and width
        probe = ffmpeg.probe(args.input)
        video_streams = [stream for stream in probe['streams'] if stream['codec_type'] == 'video']
        width = video_streams[0]['width']
        height = video_streams[0]['height']

        # set up frame decoder
        decoder = (
            ffmpeg
                .input(args.input)
                .output('pipe:', format='rawvideo', pix_fmt='rgb24', loglevel='warning')
                .run_async(pipe_stdin=True, pipe_stdout=True, cmd=args.ffmpeg_bin)
        )  # yapf: disable  # noqa
    else:
        from PIL import Image
        tmp_img = Image.open(paths[0])
        width, height = tmp_img.size
        idx = 0

    out_width, out_height = int(width * args.outscale), int(height * args.outscale)
    if out_height > 2160:
        print('You are generating video that is larger than 4K, which will be very slow due to IO speed.',
              'We highly recommend to decrease the outscale(aka, -s).')
    # encoder
    if is_video:
        audio = ffmpeg.input(args.input).audio
        encoder = (
            ffmpeg
                .input('pipe:', format='rawvideo', pix_fmt='rgb24', s=f'{out_width}x{out_height}', framerate=args.fps)
                .output(audio, video_save_path, pix_fmt='yuv420p', vcodec='libx264', loglevel='info', acodec='copy')
                .overwrite_output()
                .run_async(pipe_stdin=True, pipe_stdout=True, cmd=args.ffmpeg_bin)
        )  # yapf: disable  # noqa
    else:
        encoder = (
            ffmpeg
                .input('pipe:', format='rawvideo', pix_fmt='rgb24', s=f'{out_width}x{out_height}', framerate=args.fps)
                .output(video_save_path, pix_fmt='yuv420p', vcodec='libx264', loglevel='info')
                .overwrite_output()
                .run_async(pipe_stdin=True, pipe_stdout=True, cmd=args.ffmpeg_bin)
        )  # yapf: disable  # noqa

    while True:
        if is_video:
            img_bytes = decoder.stdout.read(width * height * 3)  # 3 bytes for one pixel
            if not img_bytes:
                break
            img = np.frombuffer(img_bytes, np.uint8).reshape([height, width, 3])
        else:
            if idx >= len(paths):
                break
            img = cv2.imread(paths[idx])
            idx += 1

        try:
            if args.face_enhance:
                _, _, output = face_enhancer.enhance(img, has_aligned=False, only_center_face=False, paste_back=True)
            else:
                output, _ = upsampler.enhance(img, outscale=args.outscale)
        except RuntimeError as error:
            print('Error', error)
            print('If you encounter CUDA out of memory, try to set --tile with a smaller number.')
        else:
            output = output.astype(np.uint8).tobytes()
            encoder.stdin.write(output)

        torch.cuda.synchronize()

    if is_video:
        decoder.stdin.close()
        decoder.wait()
    encoder.stdin.close()
    encoder.wait()


def inference_frames(args, upsampler, face_enhancer):
    is_video, paths = get_frames(args, extract_frames=True)
    video_name = os.path.splitext(os.path.basename(args.input))[0]

    # for saving restored frames
    save_frame_folder = os.path.join(args.output, video_name, 'frames_tmpout')
    os.makedirs(save_frame_folder, exist_ok=True)

    timer = AvgTimer()
    timer.start()
    pbar = tqdm(total=len(paths), unit='frame', desc='inference')
    # set up prefetch reader
    reader = PrefetchReader(paths, num_prefetch_queue=4)
    reader.start()

    que = queue.Queue()
    consumers = [IOConsumer(args, que, f'IO_{i}') for i in range(args.consumer)]
    for consumer in consumers:
        consumer.start()

    for idx, (path, img) in enumerate(zip(paths, reader)):
        imgname, extension = os.path.splitext(os.path.basename(path))
        if len(img.shape) == 3 and img.shape[2] == 4:
            img_mode = 'RGBA'
        else:
            img_mode = None

        try:
            if args.face_enhance:
                _, _, output = face_enhancer.enhance(img, has_aligned=False, only_center_face=False, paste_back=True)
            else:
                output, _ = upsampler.enhance(img, outscale=args.outscale)
        except RuntimeError as error:
            print('Error', error)
            print('If you encounter CUDA out of memory, try to set --tile with a smaller number.')

        else:
            if args.ext == 'auto':
                extension = extension[1:]
            else:
                extension = args.ext
            if img_mode == 'RGBA':  # RGBA images should be saved in png format
                extension = 'png'
            save_path = os.path.join(save_frame_folder, f'{imgname}_out.{extension}')

            que.put({'output': output, 'save_path': save_path})

        pbar.update(1)
        torch.cuda.synchronize()
        timer.record()
        avg_fps = 1. / (timer.get_avg_time() + 1e-7)
        pbar.set_description(f'idx {idx}, fps {avg_fps:.2f}')

    for _ in range(args.consumer):
        que.put('quit')
    for consumer in consumers:
        consumer.join()
    pbar.close()

    # merge frames to video
    video_save_path = os.path.join(args.output, f'{video_name}_{args.suffix}.mp4')
    os.system(f'ffmpeg -r {args.fps} -i {save_frame_folder}/frame%08d_out.{extension} -i {args.input}'
              f' -map 0:v:0 -map 1:a:0 -c:a copy -c:v libx264 -r {args.fps} -pix_fmt yuv420p  {video_save_path}')
    # delete tmp file
    shutil.rmtree(save_frame_folder)
    frame_folder = os.path.join('tmp_frames', video_name)
    if os.path.isdir(frame_folder):
        shutil.rmtree(frame_folder)


def main():
    """Inference demo for Real-ESRGAN.
    It mainly for restoring anime videos.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input', type=str, default='inputs', help='Input video, image or folder')
    parser.add_argument(
        '-n',
        '--model_name',
        type=str,
        default='realesr-animevideov3',
        help=('Model names: realesr-animevideov3 | RealESRGAN_x4plus_anime_6B | RealESRGAN_x4plus | RealESRNet_x4plus |'
              ' RealESRGAN_x2plus | '
              'Default:realesr-animevideov3'))
    parser.add_argument('-o', '--output', type=str, default='results', help='Output folder')
    parser.add_argument('-s', '--outscale', type=float, default=4, help='The final upsampling scale of the image')
    parser.add_argument('--suffix', type=str, default='out', help='Suffix of the restored video')
    parser.add_argument('-t', '--tile', type=int, default=0, help='Tile size, 0 for no tile during testing')
    parser.add_argument('--tile_pad', type=int, default=10, help='Tile padding')
    parser.add_argument('--pre_pad', type=int, default=0, help='Pre padding size at each border')
    parser.add_argument('--face_enhance', action='store_true', help='Use GFPGAN to enhance face')
    parser.add_argument(
        '--fp32', action='store_true', help='Use fp32 precision during inference. Default: fp16 (half precision).')
    parser.add_argument('--fps', type=float, default=None, help='FPS of the output video')
    parser.add_argument('--consumer', type=int, default=4, help='Number of IO consumers')
    parser.add_argument('--stream', action='store_true')
    parser.add_argument('--ffmpeg_bin', type=str, default='ffmpeg', help='The path to ffmpeg')

    parser.add_argument(
        '--alpha_upsampler',
        type=str,
        default='realesrgan',
        help='The upsampler for the alpha channels. Options: realesrgan | bicubic')
    parser.add_argument(
        '--ext',
        type=str,
        default='auto',
        help='Image extension. Options: auto | jpg | png, auto means using the same extension as inputs')
    args = parser.parse_args()

    args.input = args.input.rstrip('/').rstrip('\\')

    # ---------------------- determine models according to model names ---------------------- #
    args.model_name = args.model_name.split('.pth')[0]
    if args.model_name in ['RealESRGAN_x4plus', 'RealESRNet_x4plus']:  # x4 RRDBNet model
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)
        netscale = 4
    elif args.model_name in ['RealESRGAN_x4plus_anime_6B']:  # x4 RRDBNet model with 6 blocks
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=4)
        netscale = 4
    elif args.model_name in ['RealESRGAN_x2plus']:  # x2 RRDBNet model
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=2)
        netscale = 2
    elif args.model_name in ['realesr-animevideov3']:  # x4 VGG-style model (XS size)
        model = SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64, num_conv=16, upscale=4, act_type='prelu')
        netscale = 4

    # ---------------------- determine model paths ---------------------- #
    model_path = os.path.join('experiments/pretrained_models', args.model_name + '.pth')
    if not os.path.isfile(model_path):
        model_path = os.path.join('realesrgan/weights', args.model_name + '.pth')
    if not os.path.isfile(model_path):
        raise ValueError(f'Model {args.model_name} does not exist.')

    # restorer
    upsampler = RealESRGANer(
        scale=netscale,
        model_path=model_path,
        model=model,
        tile=args.tile,
        tile_pad=args.tile_pad,
        pre_pad=args.pre_pad,
        half=not args.fp32)

    if 'anime' in args.model_name and args.face_enhance:
        print('face_enhance is not supported in anime models, we turned this option off for you. '
              'if you insist on turning it on, please manually comment the relevant lines of code.')
        args.face_enhance = False

    if args.face_enhance:  # Use GFPGAN for face enhancement
        from gfpgan import GFPGANer
        face_enhancer = GFPGANer(
            model_path='https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.3.pth',
            upscale=args.outscale,
            arch='clean',
            channel_multiplier=2,
            bg_upsampler=upsampler)
    else:
        face_enhancer = None

    os.makedirs(args.output, exist_ok=True)

    if args.stream:
        inference_stream(args, upsampler, face_enhancer)
    else:
        inference_frames(args, upsampler, face_enhancer)


if __name__ == '__main__':
    main()
