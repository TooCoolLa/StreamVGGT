# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
import pycolmap



# TODO: frame_idx should start from 1 instead of 0 in colmap


def batch_matrix_to_pycolmap(
    points3d,
    extrinsics,
    intrinsics,
    tracks,
    image_size,
    masks=None,
    max_reproj_error=None,
    max_points3D_val=3000,
    shared_camera=False,
    camera_type="SIMPLE_PINHOLE",
    extra_params=None,
    min_inlier_per_frame=64
):
    """
    Convert Batched Pytorch Tensors to PyCOLMAP

    Check https://github.com/colmap/pycolmap for more details about its format
    """

    # points3d: Px3
    # extrinsics: Nx3x4
    # intrinsics: Nx3x3
    # tracks: NxPx2
    # masks: NxP
    # image_size: 2, assume all the frames have been padded to the same size
    # where N is the number of frames and P is the number of tracks

    N, P, _ = tracks.shape
    assert len(extrinsics) == N
    assert len(intrinsics) == N
    assert len(points3d) == P
    assert image_size.shape[0] == 2

    if max_reproj_error is not None:
        projected_points_2d, projected_points_cam = project_3D_points(points3d, extrinsics, intrinsics, return_points_cam=True)
        projected_diff = (projected_points_2d - tracks).norm(dim=-1)
        projected_points_2d[projected_points_cam[:, -1] <= 0] = 1e6
        reproj_mask = projected_diff < max_reproj_error

    if masks is not None and reproj_mask is not None:
        masks = torch.logical_and(masks, reproj_mask)
    elif masks is not None:
        masks = masks
    else:
        masks = reproj_mask

    assert masks is not None
    
    
    if masks.sum(1).min() < min_inlier_per_frame:
        print(f"Not enough inliers per frame, skip BA for this sequence.")
        return None, None
    
    extrinsics = extrinsics.cpu().numpy()
    intrinsics = intrinsics.cpu().numpy()

    if extra_params is not None:
        extra_params = extra_params.cpu().numpy()


    tracks = tracks.cpu().numpy()
    points3d = points3d.cpu().numpy()
    image_size = image_size.cpu().numpy()

    # Reconstruction object, following the format of PyCOLMAP/COLMAP
    reconstruction = pycolmap.Reconstruction()

    masks = masks.cpu().numpy()

    inlier_num = masks.sum(0)
    valid_mask = inlier_num >= 2  # a track is invalid if without two inliers
    valid_idx = np.nonzero(valid_mask)[0]

    # Only add 3D points that have sufficient 2D points
    for vidx in valid_idx:
        reconstruction.add_point3D(
            points3d[vidx], pycolmap.Track(), np.zeros(3)
        )

    num_points3D = len(valid_idx)
    camera = None
    # frame idx
    for fidx in range(N):
        # set camera
        if camera is None or (not shared_camera):
            if camera_type == "PINHOLE":
                pycolmap_intri = np.array(
                    [
                        intrinsics[fidx][0, 0],
                        intrinsics[fidx][1, 1],
                        intrinsics[fidx][0, 2],
                        intrinsics[fidx][1, 2],
                    ]
                )
            elif camera_type == "SIMPLE_RADIAL":
                focal = (intrinsics[fidx][0, 0] + intrinsics[fidx][1, 1]) / 2
                pycolmap_intri = np.array(
                    [
                        focal,
                        intrinsics[fidx][0, 2],
                        intrinsics[fidx][1, 2],
                        extra_params[fidx][0],
                    ]
                )
            elif camera_type == "SIMPLE_PINHOLE":
                focal = (intrinsics[fidx][0, 0] + intrinsics[fidx][1, 1]) / 2
                pycolmap_intri = np.array(
                    [
                        focal,
                        intrinsics[fidx][0, 2],
                        intrinsics[fidx][1, 2],
                    ]
                )
            else:
                raise ValueError(
                    f"Camera type {camera_type} is not supported yet"
                )

            camera = pycolmap.Camera(
                model=camera_type,
                width=image_size[0],
                height=image_size[1],
                params=pycolmap_intri,
                camera_id=fidx,
            )

            # add camera
            reconstruction.add_camera(camera)

        # set image
        cam_from_world = pycolmap.Rigid3d(
            pycolmap.Rotation3d(extrinsics[fidx][:3, :3]),
            extrinsics[fidx][:3, 3],
        )  # Rot and Trans
        image = pycolmap.Image(
            id=fidx,
            name=f"image_{fidx}",
            camera_id=camera.camera_id,
            cam_from_world=cam_from_world,
        )

        points2D_list = []

        point2D_idx = 0
        # NOTE point3D_id start by 1
        for point3D_id in range(1, num_points3D + 1):
            original_track_idx = valid_idx[point3D_id - 1]

            if (
                reconstruction.points3D[point3D_id].xyz < max_points3D_val
            ).all():
                if masks[fidx][original_track_idx]:
                    # It seems we don't need +0.5 for BA
                    point2D_xy = tracks[fidx][original_track_idx]
                    # Please note when adding the Point2D object
                    # It not only requires the 2D xy location, but also the id to 3D point
                    points2D_list.append(
                        pycolmap.Point2D(point2D_xy, point3D_id)
                    )

                    # add element
                    track = reconstruction.points3D[point3D_id].track
                    track.add_element(fidx, point2D_idx)
                    point2D_idx += 1

        assert point2D_idx == len(points2D_list)

        try:
            image.points2D = pycolmap.ListPoint2D(points2D_list)
            image.registered = True
        except:
            print(f"frame {fidx} is out of BA")
            image.registered = False

        # add image
        reconstruction.add_image(image)

    return reconstruction, valid_mask


def pycolmap_to_batch_matrix(
    reconstruction, device="cuda", camera_type="SIMPLE_PINHOLE"
):
    """
    Convert a PyCOLMAP Reconstruction Object to batched PyTorch tensors.

    Args:
        reconstruction (pycolmap.Reconstruction): The reconstruction object from PyCOLMAP.
        device (str): The device to place the tensors on (default: "cuda").
        camera_type (str): The type of camera model used (default: "SIMPLE_PINHOLE").

    Returns:
        tuple: A tuple containing points3D, extrinsics, intrinsics, and optionally extra_params.
    """

    num_images = len(reconstruction.images)
    max_points3D_id = max(reconstruction.point3D_ids())
    points3D = np.zeros((max_points3D_id, 3))

    for point3D_id in reconstruction.points3D:
        points3D[point3D_id - 1] = reconstruction.points3D[point3D_id].xyz
    points3D = torch.from_numpy(points3D).to(device)

    extrinsics = []
    intrinsics = []

    extra_params = [] if camera_type == "SIMPLE_RADIAL" else None

    for i in range(num_images):
        # Extract and append extrinsics
        pyimg = reconstruction.images[i]
        pycam = reconstruction.cameras[pyimg.camera_id]
        matrix = pyimg.cam_from_world.matrix()
        extrinsics.append(matrix)

        # Extract and append intrinsics
        calibration_matrix = pycam.calibration_matrix()
        intrinsics.append(calibration_matrix)

        if camera_type == "SIMPLE_RADIAL":
            extra_params.append(pycam.params[-1])

    # Convert lists to torch tensors
    extrinsics = torch.from_numpy(np.stack(extrinsics)).to(device)

    intrinsics = torch.from_numpy(np.stack(intrinsics)).to(device)

    if camera_type == "SIMPLE_RADIAL":
        extra_params = torch.from_numpy(np.stack(extra_params)).to(device)
        extra_params = extra_params[:, None]

    return points3D, extrinsics, intrinsics, extra_params





def project_3D_points(
    points3D,
    extrinsics,
    intrinsics=None,
    extra_params=None,
    return_points_cam=False,
    default=0,
    only_points_cam=False,
):
    """
    Transforms 3D points to 2D using extrinsic and intrinsic parameters.
    Args:
        points3D (torch.Tensor): 3D points of shape Px3.
        extrinsics (torch.Tensor): Extrinsic parameters of shape Bx3x4.
        intrinsics (torch.Tensor): Intrinsic parameters of shape Bx3x3.
        extra_params (torch.Tensor): Extra parameters of shape BxN, which is used for radial distortion.
    Returns:
        torch.Tensor: Transformed 2D points of shape BxNx2.
    """
    with torch.cuda.amp.autocast(dtype=torch.double):
        N = points3D.shape[0]  # Number of points
        B = extrinsics.shape[0]  # Batch size, i.e., number of cameras
        points3D_homogeneous = torch.cat(
            [points3D, torch.ones_like(points3D[..., 0:1])], dim=1
        )  # Nx4
        # Reshape for batch processing
        points3D_homogeneous = points3D_homogeneous.unsqueeze(0).expand(
            B, -1, -1
        )  # BxNx4

        # Step 1: Apply extrinsic parameters
        # Transform 3D points to camera coordinate system for all cameras
        points_cam = torch.bmm(
            extrinsics, points3D_homogeneous.transpose(-1, -2)
        )

        if only_points_cam:
            return points_cam

        # Step 2: Apply intrinsic parameters and (optional) distortion
        points2D = img_from_cam(intrinsics, points_cam, extra_params)

        if return_points_cam:
            return points2D, points_cam
        return points2D


def img_from_cam(intrinsics, points_cam, extra_params=None, default=0.0):
    """
    Applies intrinsic parameters and optional distortion to the given 3D points.

    Args:
        intrinsics (torch.Tensor): Intrinsic camera parameters of shape Bx3x3.
        points_cam (torch.Tensor): 3D points in camera coordinates of shape Bx3xN.
        extra_params (torch.Tensor, optional): Distortion parameters of shape BxN, where N can be 1, 2, or 4.
        default (float, optional): Default value to replace NaNs in the output.

    Returns:
        points2D (torch.Tensor): 2D points in pixel coordinates of shape BxNx2.
    """

    # Normalize by the third coordinate (homogeneous division)
    points_cam = points_cam / points_cam[:, 2:3, :]
    # Extract uv
    uv = points_cam[:, :2, :]

    # Apply distortion if extra_params are provided
    if extra_params is not None:
        uu, vv = apply_distortion(extra_params, uv[:, 0], uv[:, 1])
        uv = torch.stack([uu, vv], dim=1)

    # Prepare points_cam for batch matrix multiplication
    points_cam_homo = torch.cat(
        (uv, torch.ones_like(uv[:, :1, :])), dim=1
    )  # Bx3xN
    # Apply intrinsic parameters using batch matrix multiplication
    points2D_homo = torch.bmm(intrinsics, points_cam_homo)  # Bx3xN

    # Extract x and y coordinates
    points2D = points2D_homo[:, :2, :]  # Bx2xN

    # Replace NaNs with default value
    points2D = torch.nan_to_num(points2D, nan=default)

    return points2D.transpose(1, 2)  # BxNx2
