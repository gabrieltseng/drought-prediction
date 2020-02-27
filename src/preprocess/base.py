from pathlib import Path
import xarray as xr
import numpy as np

from typing import List, Optional

from ..utils import Region, region_lookup
from .utils import select_bounding_box

__all__ = ["BasePreProcessor", "Region"]

xesmf = None


class BasePreProcessor:
    r"""Base for all pre-processor classes. The preprocessing classes
    are responsible for taking the raw data exports and normalizing them
    so that they can be ingested by the feature engineering class.

    This involves:

    * subsetting the ROI (default is Kenya)
    * regridding to a consistent spatial grid (pixel size / resolution)
    * resampling to a consistent time step (hourly, daily, monthly)
    * assigning coordinates to `.nc` files (latitude, longitude, time)

    All pre-processors should do this in a ``process`` function.

    :param data_folder: The location of the data folder. Default = ``Path("data")``.
    """

    dataset: str
    static: bool = False
    analysis: bool = False

    def __init__(
        self, data_folder: Path = Path("data"), output_name: Optional[str] = None
    ) -> None:

        global xesmf
        if xesmf is None:
            import xesmf
        self.data_folder = data_folder
        self.raw_folder = self.data_folder / "raw"
        self.preprocessed_folder = self.data_folder / "interim"

        if not self.preprocessed_folder.exists():
            self.preprocessed_folder.mkdir(exist_ok=True, parents=True)

        try:
            if output_name is None:
                output_name = self.dataset

            if self.static:
                folder_prefix = f"static/{output_name}"
            else:
                folder_prefix = output_name

            if self.analysis:
                self.out_dir = (
                    self.data_folder / "analysis" / f"{folder_prefix}_preprocessed"
                )
            else:
                self.out_dir = (
                    self.preprocessed_folder / f"{folder_prefix}_preprocessed"
                )

            if not self.out_dir.exists():
                self.out_dir.mkdir(parents=True)

            self.interim = self.preprocessed_folder / f"{folder_prefix}_interim"
            if not self.interim.exists():
                self.interim.mkdir(parents=True)
        except AttributeError:
            print(
                "A dataset attribute must be added for "
                "the interim and out directories to be created"
            )

    def get_filepaths(self, folder: str = "raw") -> List[Path]:
        if folder == "raw":
            target_folder = self.raw_folder / self.dataset
        else:
            target_folder = self.interim
        outfiles = list(target_folder.glob("**/*.nc"))
        outfiles.sort()
        return outfiles

    def regrid(
        self,
        ds: xr.Dataset,
        reference_ds: xr.Dataset,
        method: str = "nearest_s2d",
        reuse_weights: bool = False,
        clean: bool = True,
    ) -> xr.Dataset:
        r""" Use xEMSF package to regrid ds to the same grid as reference_ds

        :param ds: The dataset to be regridded
        :param reference_ds: The reference dataset, onto which `ds` will be regridded
        :param method: One of ``{"bilinear", "conservative", "nearest_s2d", "nearest_d2s", "patch"}``.
            The method applied for the regridding
        :param reuse_weights: Whether to reuse the weights (weights must already be saved).
            May speed up the regridder. Default = ``False``.
        :param clean: Whether to delete the weight file. Default = ``True``.

        :return: The regridded dataset.
        """

        assert ("lat" in reference_ds.dims) & (
            "lon" in reference_ds.dims
        ), f"Need (lat,lon) in reference_ds dims Currently: {reference_ds.dims}"
        assert ("lat" in ds.dims) & (
            "lon" in ds.dims
        ), f"Need (lat,lon) in ds dims Currently: {ds.dims}"

        regridding_methods = [
            "bilinear",
            "conservative",
            "nearest_s2d",
            "nearest_d2s",
            "patch",
        ]
        assert (
            method in regridding_methods
        ), f"{method} not an acceptable regridding method. Must be one of {regridding_methods}"

        # create the grid you want to convert TO (from reference_ds)
        ds_out = xr.Dataset(
            {"lat": (["lat"], reference_ds.lat), "lon": (["lon"], reference_ds.lon)}
        )

        shape_in = len(ds.lat), len(ds.lon)
        shape_out = len(reference_ds.lat), len(reference_ds.lon)
        # unique id so when parallel process doesn't write to same file
        uid = f"{np.random.rand(1)[0]:.2f}"

        # The weight file should be deleted by regridder.clean_weight_files(), but in case
        # something goes wrong and its not, lets use a descriptive filename
        if reuse_weights:
            # if not running in parallel can save time by reusing weights
            filename = f"{method}_{shape_in[0]}x{shape_in[1]}_\
            {shape_out[0]}x{shape_out[1]}.nc".replace(
                " ", ""
            )
        else:
            filename = f"{method}_{shape_in[0]}x{shape_in[1]}_\
            {shape_out[0]}x{shape_out[1]}_{uid}.nc".replace(
                " ", ""
            )
        savedir = self.preprocessed_folder / filename

        regridder = xesmf.Regridder(  # type: ignore
            ds, ds_out, method, filename=str(savedir), reuse_weights=reuse_weights
        )

        variables = [v for v in ds.data_vars]
        output_dict = {}
        for var in variables:
            print(f"- regridding var {var} -")
            output_dict[var] = regridder(ds[var])
        ds = xr.Dataset(output_dict)

        print(
            f"Regridded from {(regridder.Ny_in, regridder.Nx_in)} "
            f"to {(regridder.Ny_out, regridder.Nx_out)}"
        )

        if clean:
            regridder.clean_weight_file()

        return ds

    @staticmethod
    def load_reference_grid(path_to_grid: Path) -> xr.Dataset:
        r"""Since the regridder only needs to the lat and lon values,
        there is no need to pass around an enormous grid for the regridding.

        In fact, only the latitude and longitude values are necessary!

        :param path_to_grid: A path to the reference dataset.
        :returns: The loaded reference dataset, but with only the latitudes and longitudes.
        """
        full_dataset = xr.open_dataset(path_to_grid)

        assert {"lat", "lon"} <= set(
            full_dataset.dims
        ), "Dimensions named lat and lon must be in the reference grid"
        return full_dataset[["lat", "lon"]]

    @staticmethod
    def resample_time(
        ds: xr.Dataset,
        resample_length: str = "M",
        upsampling: bool = False,
        time_coord: str = "time",
    ) -> xr.Dataset:
        # TODO: would be nice to programmatically get upsampling / not
        ds = ds.sortby(time_coord)

        resampler = ds.resample({time_coord: resample_length})

        if not upsampling:
            return resampler.mean()
        else:
            return resampler.nearest()

    @staticmethod
    def chop_roi(
        ds: xr.Dataset, subset_str: Optional[str] = "kenya", inverse_lat: bool = False
    ) -> xr.Dataset:
        """Select a geographical subset of the data, based on a subset string.

        :param ds: The dataset to be subsetted.
        :param subset_str: Defines a geographical subset of the downloaded data to be used.
            Should be one of the regions defined in ``src.utils.region_lookup``.
            Default = ``"kenya"``.
        :inverse_lat: Whether to inverse the minimum and maximum longitudes. Default = ``False``.
        """
        region = region_lookup[subset_str] if subset_str is not None else None
        if region is not None:
            ds = select_bounding_box(ds, region, inverse_lat=inverse_lat)

        return ds

    def merge_files(
        self,
        subset_str: Optional[str] = "kenya",
        resample_time: Optional[str] = "M",
        upsampling: bool = False,
        filename: Optional[str] = None,
    ) -> None:
        r"""Merge multiple interim files into a single preprocessed file. The time resampling happens here,
        since all the data is necessary to do that.

        :param subset_str: The optional subset string used to get a geographical subset of the data.
            Only used to make a more descriptive filename.
        :param resample_length: Defines the time length to which the data will be resampled.
        :param upsampling: If true, tells the class the time-sampling will be upsampling. In this case,
            nearest instead of mean is used for the resampling. Default = ``False``.
        :param filename: Override the default created filename by passing a ``string`` filename here.
        """

        ds = xr.open_mfdataset(self.get_filepaths("interim"))

        if resample_time is not None:
            ds = self.resample_time(ds, resample_time, upsampling)

        if filename is None:
            filename = f'data{"_" + subset_str if subset_str is not None else ""}.nc'
        out = self.out_dir / filename

        ds.to_netcdf(out)
        print(f"\n**** {out} Created! ****\n")
