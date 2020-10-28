import json
import logging as lg
import math
import tempfile
import time
import uuid
from json import JSONDecodeError
from sqlite3.dbapi2 import connect
from zipfile import ZipFile

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from epw import epw as Epw
from geopandas import GeoDataFrame
from osmnx.settings import default_crs
from path import Path
from rhino3dm import (
    Brep,
    Extrusion,
    File3dm,
    Line,
    ObjectAttributes,
    Plane,
    Point3d,
    Point3dList,
    PolylineCurve,
)
from rhino3dm._rhino3dm import UnitSystem
from shapely.geometry.polygon import orient
from tqdm import tqdm

from pyumi.umi_layers import UmiLayers


def geom_to_curve(feature):
    """Converts the GeoSeries to a :class:`_file3dm.PolylineCurve`

    Args:
        feature (GeoSeries):

    Returns:
        PolylineCurve
    """

    return PolylineCurve(
        Point3dList([Point3d(x, y, 0) for x, y, *z in feature.geometry.exterior.coords])
    )


def geom_to_brep(feature, height_column_name):
    """Converts the Shapely :class:`shapely.geometry.base.BaseGeometry` to
    a :class:`_file3dm.Brep`.

    Args:
        feature (GeoSeries): A GeoSeries containing a `geometry` column.
        height_column_name (str): Name of the column containing the height
            attribute.

    Returns:
        Brep: The Brep
    """
    # Converts the GeoSeries to a :class:`_file3dm.PolylineCurve`
    feature.geometry = orient(feature.geometry, sign=1.0)
    height = feature[height_column_name]

    outerProfile = PolylineCurve(
        Point3dList([Point3d(x, y, 0) for x, y, *z in feature.geometry.exterior.coords])
    )
    innerProfiles = []
    for interior in feature.geometry.interiors:
        innerProfiles.append(
            PolylineCurve(
                Point3dList([Point3d(x, y, 0) for x, y, *z in interior.coords[::1]])
            )
        )

    if outerProfile is None or height <= 1e-12:
        return np.NaN

    plane = Plane.WorldXY()
    if not plane:
        return np.NaN

    path = Line(Point3d(0, 0, 0), Point3d(0, 0, height))
    if not path.IsValid or path.Length <= 1e-12:
        return np.NaN

    up = plane.YAxis
    curve = outerProfile.Duplicate()
    curve.ChangeDimension(2)

    extrusion = Extrusion()  # Initialize the Extrusion
    extrusion.SetOuterProfile(curve, True)  # Sets the outer profile

    # Sets the inner profiles, if they exist
    for profile in innerProfiles:
        curve = profile.Duplicate()
        curve.ChangeDimension(2)
        extrusion.AddInnerProfile(curve)

    # Set Path and Up
    extrusion.SetPathAndUp(path.From, path.To, up)

    # Transform extrusion to Brep
    brep = extrusion.ToBrep(False)

    return brep


class UmiProject:
    """An UMI Project

    Attributes:
        to_crs (dict): The
        gdf_world (GeoDataFrame): GeoDataFrame in original world coordinates
        gdf_3dm (GeoDataFrame): GeoDataFrame in projected coordinates and
            translated to Rhino origin (0,0,0).

    """

    DEFAULT_SHOEBOX_SETTINGS = {
        "CoreDepth": 3,
        "Envr": 1,
        "Fdist": 0.01,
        "FloorToFloorHeight": 3.0,
        "PerimeterOffset": 3.0,
        "RoomWidth": 3.0,
        "WindowToWallRatioE": 0.4,
        "WindowToWallRatioN": 0.4,
        "WindowToWallRatioRoof": 0,
        "WindowToWallRatioS": 0.4,
        "WindowToWallRatioW": 0.4,
        "TemplateName": np.NaN,
        "EnergySimulatorName": "UMI Shoeboxer (default)",
        "FloorToFloorStrict": True,
    }

    def __init__(
        self, project_name="unnamed", epw=None, template_lib=None, file3dm=None
    ):
        """An UmiProject package containing the _file3dm file, the project
        settings, the umi.sqlite3 database.

        Args:
            project_name (str): The name of the project
            epw (str or Path or Epw): Path of the weather file or Epw object.
            template_lib (str or Path):
        """

        self.project_settings = {}
        self.to_crs = default_crs
        self.gdf_world = GeoDataFrame()
        self.gdf_world_projected = GeoDataFrame()
        self.gdf_3dm = GeoDataFrame()
        self.tmp = Path(tempfile.mkdtemp(dir=Path("")))

        self.name = project_name
        self.file3dm = file3dm or File3dm()
        self.template_lib = template_lib
        self.epw = epw

        # Initiate Layers in 3dm file
        self.umiLayers = UmiLayers(self.file3dm)

        with connect(self.tmp / "umi.sqlite3") as con:
            con.execute(create_nonplottable_setting)
            con.execute(create_object_name_assignement)
            con.execute(create_plottatble_setting)
            con.execute(create_series)
            con.execute(create_data_point)

        # Set ModelUnitSystem to Meters
        self.file3dm.Settings.ModelUnitSystem = UnitSystem.Meters

        self.umi_sqlite3 = con

    @property
    def epw(self):
        return self._epw

    @epw.setter
    def epw(self, value):
        """
        Args:
            value:
        """
        if value:
            if isinstance(value, Epw):
                self._epw = value
            set_epw = Path(value).expand()
            if set_epw.exists() and set_epw.endswith(".epw"):
                # try remove if already there
                (self.tmp / set_epw.basename()).remove_p()
                # copy to self.tmp
                tmp_epw = set_epw.copy(self.tmp)
                # set attr value
                self._epw = tmp_epw
        else:
            self._epw = None

    @property
    def template_lib(self):
        return self._template_lib

    @template_lib.setter
    def template_lib(self, value):
        if value:
            set_lib = Path(value).expand()
            if set_lib.exists() and set_lib.endswith(".json"):
                # try remove if already there
                (self.tmp / set_lib.basename()).remove_p()
                # copy to self.tmp
                tmp_lib = set_lib.copy(self.tmp)
                # set attr value
                self._template_lib = tmp_lib
        else:
            self._template_lib = None

    def __del__(self):
        self.umi_sqlite3.close()
        self.tmp.rmtree_p()

    @classmethod
    def from_gis(
        cls,
        input_file,
        height_column_name,
        epw,
        template_lib,
        template_map,
        map_to_column,
        fid=None,
        to_crs=None,
        **kwargs,
    ):
        """Returns an UMI project by reading a GIS file (Shapefile, GeoJson,
        etc.). A height attribute must be passed in order to extrude the
        building footprints to their height. All buildings will have an
        elevation of 0 m. The input file is reprojected to :attr:`to_crs`
        (defaults to 'epsg:3857') and the extent is moved to the origin
        coordinates.

        Args:
            input_file (str or Path): Path to the GIS file. A zipped file
                can be passed by appending the path with "zip:/". Any file
                type read by :meth:`geopandas.io.file._read_file` is
                compatible.
            height_column_name (str): The attribute name containing the
                height values. Missing values will be ignored.
            fid (str): Optional, the column name corresponding to the id of
                each feature. If None, a serial id is created automatically.
            to_crs (dict): The output CRS to which the file will be
                projected to. Units must be meters.
            **kwargs: keyword arguments passed to UmiProject constructor.

        Returns:
            UmiProject: The UmiProject. Needs to be saved
        """
        input_file = Path(input_file)

        # First, load the file to a GeoDataFrame
        start_time = time.time()
        lg.info("reading input file...")
        gdf = gpd.read_file(input_file)
        lg.info(
            f"Read {gdf.memory_usage(index=True).sum() / 1000:,.1f}KB from"
            f" {input_file} in"
            f" {time.time()-start_time:,.2f} seconds"
        )
        if "name" not in kwargs:
            kwargs["name"] = input_file.stem

        # Assign template names using map. Changes elements based on the
        # chosen column name parameter.
        def on_frame(map_to_column, template_map):
            """Returns the DataFrame for left_join based on number of
            nested levels"""
            depth = dict_depth(template_map)
            if depth == 2:
                return (
                    pd.Series(template_map)
                    .rename_axis(map_to_column)
                    .rename("TemplateName")
                    .to_frame()
                )
            elif depth == 3:
                return (
                    pd.DataFrame(template_map)
                    .stack()
                    .swaplevel()
                    .rename_axis(map_to_column)
                    .rename("TemplateName")
                    .to_frame()
                )
            elif depth == 4:
                return (
                    pd.DataFrame(template_map)
                    .stack()
                    .swaplevel()
                    .apply(pd.Series)
                    .stack()
                    .rename_axis(map_to_column)
                    .rename("TemplateName")
                    .to_frame()
                )
            else:
                raise NotImplementedError("5 levels or more are not yet supported")

        _index = gdf.index
        gdf = gdf.set_index(map_to_column).join(
            on_frame(map_to_column, template_map), on=map_to_column
        )
        gdf.index = _index

        return cls.from_gdf(
            gdf, height_column_name, epw, template_lib, to_crs, fid, **kwargs
        )

    @classmethod
    def from_gdf(
        cls,
        gdf,
        height_column_name,
        epw,
        template_lib,
        to_crs=None,
        fid=None,
        **kwargs,
    ):
        """Returns an UMI project by reading a GeoDataFrame. A height
        attribute must be passed in order to extrude the
        building footprints to their height. All buildings will have an
        elevation of 0 m. The GeoDataFrame must be projected and the extent
        is moved to the origin coordinates.

        Args:
            input_file (str or Path): Path to the GIS file. A zipped file
                can be passed by appending the path with "zip:/". Any file
                type read by :meth:`geopandas.io.file._read_file` is
                compatible.
            height_column_name (str): The attribute name containing the
                height values. Missing values will be ignored.
            to_crs (dict): The output CRS to which the file will be projected
                to. Units must be meters.
            **kwargs: keyword arguments passed to UmiProject constructor.

        Returns:
            UmiProject: The UmiProject. Needs to be saved.
        """
        # Filter rows; Display invalid geometries in log
        valid_geoms = gdf.geometry.is_valid
        if (~valid_geoms).any():
            lg.warning(
                f"Invalid geometries found! The following "
                f"{(~valid_geoms).sum()} entries "
                f"where ignored: {gdf.loc[~valid_geoms].index}"
            )
        else:
            lg.info("No invalid geometries reported")
        gdf = gdf.loc[valid_geoms, :]  # Only valid geoms

        # Filter rows missing attribute
        valid_attrs = ~gdf[height_column_name].isna()
        if (~valid_attrs).any():
            lg.warning(
                f"Some rows have a missing {height_column_name}! The following "
                f"{(~valid_attrs).sum()} entries "
                f"where ignored: {gdf.loc[~valid_attrs].index}"
            )
        else:
            lg.info(
                f"{valid_attrs.sum()} reported features with a "
                f"{height_column_name} attribute value"
            )
        gdf = gdf.loc[valid_attrs, :]

        # Set the identification of buildings. This "fid" is used as the
        # Brep `Name` attribute. If a building is made of multiple
        # polygons, then the Breps will have the same name.
        if not fid:
            fid = "fid"
            if "fid" in gdf.columns:
                pass  # This is a user-defined fid
            else:
                gdf["fid"] = gdf.index.values  # This serial fid

        # Explode to singlepart
        gdf = gdf.explode()  # The index of the input geodataframe is no
        # longer unique and is replaced with a multi-index (original index
        # with additional level indicating the multiple geometries: a new
        # zero-based index for each single part geometry per multi-part
        # geometry).
        from osmnx.projection import project_gdf

        gdf_world = project_gdf(gdf, to_latlong=True)
        try:
            gdf = project_gdf(gdf, to_crs=to_crs)
        except ValueError:
            # Geometry is already projected. cannot calculate UTM zone
            pass
        finally:
            gdf_world_projected = gdf.copy()  # make a copy for reference

        # Move to center; Makes the Shoeboxer happy
        world_centroid = gdf_world_projected.unary_union.convex_hull.centroid
        xoff, yoff = world_centroid.x, world_centroid.y
        gdf.geometry = gdf.translate(-xoff, -yoff)

        # Create Rhino Geometries in two steps
        tqdm.pandas(desc="Creating 3D geometries")
        gdf["rhino_geom"] = gdf.progress_apply(
            geom_to_brep, args=(height_column_name,), axis=1
        )

        # Filter out errored rhino geometries
        errored_brep = gdf["rhino_geom"].isna()
        if errored_brep.any():
            lg.warning(
                f"Brep creation errors! The following "
                f"{errored_brep.sum()} entries "
                f"where ignored: {gdf.loc[errored_brep].index}"
            )
        else:
            lg.info(f"{gdf.size} breps created")
        gdf = gdf.loc[~errored_brep, :]

        # create the UmiProject object
        name = kwargs.get("name")
        umi_project = cls(project_name=name, epw=epw, template_lib=template_lib)
        umi_project.gdf_3dm = gdf
        umi_project.gdf_world = gdf_world  # assign gdf_world here
        umi_project.gdf_world_projected = gdf_world_projected
        umi_project.to_crs = gdf._crs  # assign to_crs here

        # Add all Breps to Model and append UUIDs to gdf
        tqdm.pandas(desc="Adding Breps")
        gdf["guid"] = gdf["rhino_geom"].progress_apply(
            umi_project.file3dm.Objects.AddBrep
        )
        gdf.drop(columns=["rhino_geom"], inplace=True)  # don't carry around

        for obj in umi_project.file3dm.Objects:
            obj.Attributes.LayerIndex = umi_project.umiLayers["umi::Buildings"].Index
            obj.Attributes.Name = str(
                gdf.loc[gdf.guid == obj.Attributes.Id, fid].values[0]
            )

        umi_project.add_default_shoebox_settings()

        umi_project.update_umi_sqlite3()

        umi_project.add_site_boundary()

        return umi_project

    def update_umi_sqlite3(self):
        """Updates the self.umi_sqlite3 with self.gdf_3dm

        Returns:
            UmiProject: self
        """
        nonplot_settings = [
            "TemplateName",
            "EnergySimulatorName",
            "FloorToFloorStrict",
        ]

        # First, update plottable settings
        _df = self.gdf_3dm.loc[
            :,
            [
                attr
                for attr in self.DEFAULT_SHOEBOX_SETTINGS
                if attr not in nonplot_settings
            ]
            + ["guid"],  # guid needed in sql
        ]
        _df = (
            (_df.melt("guid", var_name="name").rename(columns={"guid": "object_id"}))
            .astype({"object_id": "str"})
            .dropna(subset=["value"])
        )
        _df.to_sql(
            "plottable_setting",
            index=True,
            index_label="key",
            con=self.umi_sqlite3,
            if_exists="replace",
            method="multi",
        )  # write to sql, replace existing

        # Second, update non-plottable settings
        _df = self.gdf_3dm.loc[
            :,
            [attr for attr in nonplot_settings] + ["guid"],  # guid needed in sql
        ]
        _df = (
            (_df.melt("guid", var_name="name").rename(columns={"guid": "object_id"}))
            .astype({"object_id": "str"})
            .dropna(subset=["value"])
        )
        _df.to_sql(
            "nonplottable_setting",
            index=True,
            index_label="key",
            con=self.umi_sqlite3,
            if_exists="replace",
            method="multi",
        )  # write to sql, replace existing
        return self

    def add_default_shoebox_settings(self):
        """Adds default values to self.gdf_3dm. If values are already
        defined, only NaNs are replace.

        Returns:
            UmiProject: self
        """
        bldg_attributes = self.DEFAULT_SHOEBOX_SETTINGS
        # First add columns if they don't exist
        for attr in bldg_attributes:
            if attr not in self.gdf_3dm.columns:
                self.gdf_3dm[attr] = bldg_attributes[attr]

        # Then, fill NaNs with defaults, for good measure.
        self.gdf_3dm.fillna(value=bldg_attributes, inplace=True)

        return self

    def add_site_boundary(self):
        """Add Site boundary PolylineCurve. Uses the exterior of the
        convex_hull of the unary_union of all footprints. This is a good
        approximation of a site boundary in most cases.

        Returns:
            UmiProject: self
        """
        boundary = PolylineCurve(
            Point3dList(
                [
                    Point3d(x, y, 0)
                    for x, y, *z in self.gdf_3dm.geometry.unary_union.convex_hull.exterior.coords
                ]
            )
        )
        guid = self.file3dm.Objects.AddCurve(boundary)
        fileObj, *_ = filter(lambda x: x.Attributes.Id == guid, self.file3dm.Objects)
        fileObj.Attributes.LayerIndex = self.umiLayers[
            "umi::Context::Site boundary"
        ].Index
        fileObj.Attributes.Name = "Convex hull boundary"

        return self

    @classmethod
    def open(cls, filename):
        """"""
        filename = Path(filename)
        project_name = filename.stem
        # with unziped file load in the files
        with ZipFile(filename) as umizip:
            with tempfile.TemporaryDirectory() as tempdir:
                # extract and load file3dm

                umizip.extract(project_name + ".3dm", tempdir)
                file3dm = File3dm.Read(Path(tempdir) / project_name + ".3dm")

                epw_file, *_ = (file for file in umizip.namelist() if ".epw" in file)
                umizip.extract(epw_file, tempdir)
                epw = Epw()
                epw.read(Path(tempdir) / epw_file)

                tmp_lib, *_ = (
                    file
                    for file in umizip.namelist()
                    if ".json" in file and "sdl-common" not in file
                )
                with umizip.open(tmp_lib) as f:
                    template_lib = json.load(f)
                umizip.extract(epw_file, tempdir)
                epw = Epw()
                epw.read(Path(tempdir) / epw_file)

                sdl_common = {}  # prepare sdl-common dict

                # loop over 'sdl-common' config files (.json)
                for file in [
                    file for file in umizip.namelist() if "sdl-common" in file
                ]:
                    if file == "sdl-common/project.json":
                        # extract and load GeoDataFrame

                        lat, lon = epw.headers["LOCATION"][5:7]
                        utm_zone = int(math.floor((float(lon) + 180) / 6.0) + 1)
                        utm_crs = (
                            f"+proj=utm +zone={utm_zone} +ellps=WGS84 "
                            f"+datum=WGS84 +units=m +no_defs"
                        )
                        umizip.extract("sdl-common/project.json", tempdir)
                        gdf_3dm = GeoDataFrame.from_file(
                            Path(tempdir) / "sdl-common/project.json", crs=utm_crs
                        )
                    else:
                        with umizip.open(file) as f:
                            try:
                                sdl_common[Path(file).stem] = json.load(f)
                            except JSONDecodeError:
                                sdl_common[Path(file).stem] = {}

        umi_project = cls.from_gdf(
            gdf_3dm,
            "Height",
            project_name=project_name,
            epw=epw,
            fid="id",
            template_lib=template_lib,
            file3dm=file3dm,
        )
        return umi_project

    def to_file(self, filename, driver="GeoJSON", schema=None, index=None, **kwargs):
        """Write the ``UmiProject`` to another file format. The
        :attr:`UmiProject.gdf_3dm` is first translated back to the
        :attr:`UmiProject.world_gdf_projected.centroid` and then reprojected
        to the :attr:`UmiProject.world_gdf._crs`.

        By default, a GeoJSON is written, but any OGR data source
        supported by Fiona can be written. A dictionary of supported OGR
        providers is available via:

        >>> import fiona
        >>> fiona.supported_drivers

        Args:
            filename (str): File path or file handle to write to.
            driver (str): The OGR format driver used to write the vector
                file. Deaults to "GeoJSON".
            schema (dict): If specified, the schema dictionary is passed to
                Fiona to better control how the file is written.
            index (bool): If True, write index into one or more columns
                (for MultiIndex). Default None writes the index into one or
                more columns only if the index is named, is a MultiIndex,
                or has a non-integer data type. If False, no index is written.

        Notes:
            The extra keyword arguments ``**kwargs`` are passed to
            :meth:`fiona.open`and can be used to write to multi-layer data,
            store data within archives (zip files), etc.

            The format drivers will attempt to detect the encoding of your
            data, but may fail. In this case, the proper encoding can be
            specified explicitly by using the encoding keyword parameter,
            e.g. ``encoding='utf-8'``.

        Examples:
            >>> from pyumi.umi_project import UmiProject
            >>> UmiProject().to_file("project name", driver="ESRI Shapefile")
            Or
            >>> from pyumi.umi_project import UmiProject
            >>> UmiProject().to_file("project name", driver="GeoJSON")

        Returns:
            None
        """
        world_crs = self.gdf_world._crs  # get utm crs
        exp_gdf = self.gdf_3dm.copy()  # make a copy

        dtype_map = {"guid": str}
        exp_gdf.loc[:, list(dtype_map)] = exp_gdf.astype(dtype_map)

        xdiff, ydiff = self.gdf_world_projected.unary_union.centroid.coords[0]

        exp_gdf.geometry = exp_gdf.translate(xdiff, ydiff)
        # Project the gdf to the world_crs
        from osmnx import project_gdf

        exp_gdf = project_gdf(exp_gdf, world_crs)

        # Convert to file. Uses fiona
        exp_gdf.to_file(
            filename=filename, driver=driver, schema=schema, index=index, **kwargs
        )

    def save(self, filename=None):
        """Saves the UmiProject to a packaged .umi file (zipped folder)

        Args:
            filename (str or Path): Optional, the path to the destination.
                May or may not contain the extension (.umi).

        Returns:
            UmiProject: self
        """
        dst = Path(".")  # set destination as current directory
        if filename:  # a specific filename is passed
            dst = Path(filename).dirname()  # set dir path
            self.name = Path(filename).stem  # update project name

        # First, write files to tmp destination
        self.file3dm.Write(self.tmp / (self.name + ".3dm"), 6)
        self.umi_sqlite3.commit()  # commit db changes

        with open((self.tmp / "sdl-common").mkdir_p() / "project.json", "w") as common:
            if not self.gdf_3dm.empty:
                _json = self.gdf_3dm.to_json(cls=ComplexEncoder)
                response = json.loads(_json)
                json.dump(response, common, indent=3)

                for project_setting in self.project_settings:
                    json.dump(response, common, indent=3)

        # Second, loop over files in tmp folder and copy to dst
        outfile = (dst / Path(self.name) + ".umi").expand()
        with ZipFile(outfile, "w") as zip_file:
            for file in self.tmp.files():
                # write `file` to arcname `file.basename()`
                zip_file.write(file, file.basename())
            for file in (self.tmp / "sdl-common").files():
                zip_file.write(file, "sdl-common" / file.basename())

        # Todo: Save template-lib dict to file
        # Todo: Save epw object to file

        lg.info(f"Saved to {outfile.abspath()}")

        return self

    def add_street_graph(
        self,
        polygon=None,
        network_type="all_private",
        simplify=True,
        retain_all=False,
        truncate_by_edge=False,
        clean_periphery=True,
        custom_filter=None,
    ):
        """Downloads a spatial street graph from OpenStreetMap's APIs and
        transforms it to PolylineCurves to the self.file3dm document.

        Uses :ref:`osmnx` to retrieve the street graph. The same parameters
        as :met:`osmnx.graph.graph_from_polygon` are available.

        Args:
            polygon (Polygon or MultiPolygon, optional): If none, the extent
                of the project GIS dataset is used (convex hull). If not
                None, polygon is the shape to get network data within.
                coordinates should be in units of latitude-longitude degrees.
            network_type (string): what type of street network to get if
                custom_filter is None. One of 'walk', 'bike', 'drive',
                'drive_service', 'all', or 'all_private'.
            simplify (bool): if True, simplify the graph topology with the
                simplify_graph function
            retain_all (bool): if True, return the entire graph even if it
                is not connected. otherwise, retain only the largest weakly
                connected component.
            truncate_by_edge (bool): if True, retain nodes outside boundary
                polygon if at least one of node's neighbors is within the
                polygon
            clean_periphery (bool): if True, buffer 500m to get a graph
                larger than requested, then simplify, then truncate it to
                requested spatial boundaries
            custom_filter (string): a custom network filter to be used
                instead of the network_type presets, e.g.,
                '["power"~"line"]' or '["highway"~"motorway|trunk"]'. Also
                pass in a network_type that is in
                settings.bidirectional_network_types if you want graph to be
                fully bi-directional.

        Examples:
            >>> from pyumi.umi_project import UmiProject
            >>> UmiProject.from_gis().add_street_graph(
            >>>     network_type="all_private",retain_all=True,
            >>>     clean_periphery=False
            >>> ).save()

            Do not forget to save!

        Returns:
            UmiProject: self
        """
        if getattr(self, "gdf_world", None) is None:
            raise ValueError("This UmiProject does not contain a GeoDataFrame")
        import osmnx as ox

        # Configure osmnx
        ox.config(log_console=True, use_cache=True)
        if polygon is None:
            # Create the boundary polygon. Here we use the convex_hull
            # polygon : shapely.geometry.Polygon or shapely.geometry.MultiPolygon
            #           the shape to get network data within. coordinates should
            #           be in units of latitude-longitude degrees.
            polygon = self.gdf_world.to_crs("EPSG:4326").unary_union.convex_hull
        self.street_graph = ox.graph_from_polygon(
            polygon,
            network_type,
            simplify,
            retain_all,
            truncate_by_edge,
            clean_periphery,
            custom_filter,
        )
        self.street_graph = ox.project_graph(self.street_graph, self.to_crs)
        gdf_edges = ox.graph_to_gdfs(self.street_graph, nodes=False)
        gdf_edges.geometry = gdf_edges.translate(
            -self.gdf_world.unary_union.centroid.x,
            -self.gdf_world.unary_union.centroid.y,
        )

        def to_polylinecurve(series):
            """Create geometry and add to 2dm file"""
            _3dmgeom = PolylineCurve(
                Point3dList([Point3d(x, y, 0) for x, y in series.geometry.coords])
            )
            attr = ObjectAttributes()  # Initiate attributes object
            attr.LayerIndex = self.umiLayers["umi::Context::Streets"].Index  #
            # Set lyr index
            try:
                name_str_or_list = series["name"]
                if name_str_or_list and isinstance(name_str_or_list, list):
                    attr.Name = "+ ".join(name_str_or_list)  # Set Name as St. name
                elif name_str_or_list and isinstance(name_str_or_list, str):
                    attr.Name = name_str_or_list
            except KeyError:
                pass

            # Add to file3dm
            guid = self.file3dm.Objects.AddCurve(_3dmgeom, attr)
            return guid

        guids = gdf_edges.apply(to_polylinecurve, axis=1)

        return self

    def add_pois(self, polygon=None, tags=None, on_file3dm_layer=None):
        """Add points of interests (POIs) from OpenStreetMap.

        Args:
            polygon (Polygon or Multipolygon): geographic boundaries to
                fetch geometries within. Units should be in degrees.
            tags (dict): Dict of tags used for finding POIs from the selected
                area. Results returned are the union, not intersection of each
                individual tag. Each result matches at least one tag given.
                The dict keys should be OSM tags, (e.g., amenity, landuse,
                highway, etc) and the dict values should be either True to
                retrieve all items with the given tag, or a string to get a
                single tag-value combination, or a list of strings to get
                multiple values for the given tag. For example, tags = {
                ‘amenity’:True, ‘landuse’:[‘retail’,’commercial’],
                ‘highway’:’bus_stop’} would return all amenities,
                landuse=retail, landuse=commercial, and highway=bus_stop.
            on_file3dm_layer (str, or Layer): specify on which file3dm layer
                the pois will be put. Defaults to umi::Context.

        Returns:
            UmiProject: self
        """
        import osmnx as ox

        ox.config(log_console=True, use_cache=True)

        if polygon is None:
            polygon = self.gdf_world.unary_union.convex_hull

        # Retrieve the pois from OSM
        gdf = ox.geometries_from_polygon(polygon, tags=tags)
        if gdf.empty:
            lg.warning("No pois found for location. Check your tags")
            return self
        # Project to UmiProject crs
        gdf = ox.project_gdf(gdf, self.to_crs)

        # Move to 3dm origin
        gdf.geometry = gdf.translate(
            -self.gdf_world_projected.unary_union.centroid.x,
            -self.gdf_world_projected.unary_union.centroid.y,
        )

        def resolve_3dmgeom(series, on_file3dm_layer):
            geom = series.geometry  # Get the geometry

            if isinstance(geom, shapely.geometry.Point):
                # if geom is a Point
                guid = self.file3dm.Objects.AddPoint(geom.x, geom.y, 0)
                geom3dm, *_ = filter(
                    lambda x: x.Attributes.Id == guid, self.file3dm.Objects
                )
                geom3dm.Attributes.LayerIndex = on_file3dm_layer.Index
                geom3dm.Attributes.Name = str(series.osmid)
            elif isinstance(
                geom, (shapely.geometry.Polygon, shapely.geometry.MultiPolygon)
            ):
                # if geom is a Polygon
                polycurve = PolylineCurve(
                    Point3dList([Point3d(x, y, 0) for x, y, *z in geom.exterior.coords])
                )
                # This is somewhat of a hack. The surface is created by
                # trimming the WorldXY plane to a PolylineCurve.
                geom3dm = Brep.CreateTrimmedPlane(
                    Plane.WorldXY(),
                    polycurve,
                )

                # Set the pois attributes
                geom3dm_attr = ObjectAttributes()
                geom3dm_attr.LayerIndex = on_file3dm_layer.Index
                geom3dm_attr.Name = str(series.osmid)
                geom3dm_attr.ObjectColor = (205, 247, 201, 255)

                guid = self.file3dm.Objects.AddBrep(geom3dm, geom3dm_attr)
            elif isinstance(geom, shapely.geometry.MultiPolygon):
                # if geom is a MultiPolygon, iterate over
                for polygon in geom:
                    polycurve = PolylineCurve(
                        Point3dList(
                            [Point3d(x, y, 0) for x, y, *z in polygon.exterior.coords]
                        )
                    )
                    # This is somewhat of a hack. The surface is created by
                    # trimming the WorldXY plane to a PolylineCurve.
                    geom3dm = Brep.CreateTrimmedPlane(
                        Plane.WorldXY(),
                        polycurve,
                    )

                    # Set the pois attributes
                    geom3dm_attr = ObjectAttributes()
                    geom3dm_attr.LayerIndex = on_file3dm_layer.Index
                    geom3dm_attr.Name = str(series.osmid)
                    geom3dm_attr.ObjectColor = (205, 247, 201, 255)

                    guid = self.file3dm.Objects.AddBrep(geom3dm, geom3dm_attr)
            elif isinstance(geom, shapely.geometry.linestring.LineString):
                geom3dm = PolylineCurve(
                    Point3dList([Point3d(x, y, 0) for x, y, *z in geom.coords])
                )
                geom3dm_attr = ObjectAttributes()
                geom3dm_attr.LayerIndex = on_file3dm_layer.Index
                geom3dm_attr.Name = str(series.osmid)

                guid = self.file3dm.Objects.AddCurve(geom3dm, geom3dm_attr)
            else:
                raise NotImplementedError(
                    f"osmnx: geometry (osmid={series.osmid}) of type "
                    f"{type(geom)} cannot be parsed as a rhino3dm object"
                )
            return guid

        # Parse geometries
        if not on_file3dm_layer:
            on_file3dm_layer = self.umiLayers["umi::Context"]
        if isinstance(on_file3dm_layer, str):
            on_file3dm_layer = self.umiLayers[on_file3dm_layer]
        self._pois_ids = gdf.apply(resolve_3dmgeom, args=(on_file3dm_layer,), axis=1)

        return self


create_nonplottable_setting = """create table nonplottable_setting
(
    key       TEXT not null,
    object_id TEXT not null,
    name      TEXT not null,
    value     TEXT not null,
    primary key (key, object_id, name)
);"""
create_object_name_assignement = """create table object_name_assignment
(
    id   TEXT
        primary key,
    name TEXT not null
);"""
create_plottatble_setting = """create table plottable_setting
(
    key       TEXT not null,
    object_id TEXT not null,
    name      TEXT not null,
    value     REAL not null,
    primary key (key, object_id, name)
);"""
create_series = """create table series
(
    id         INTEGER primary key,
    name       TEXT not null,
    module     TEXT not null,
    object_id  TEXT not null,
    units      TEXT,
    resolution TEXT,
    unique (name, module, object_id)
);"""
create_data_point = """create table data_point
(
    series_id       INTEGER not null references series on delete cascade,
    index_in_series INTEGER not null,
    value           REAL    not null, 
    primary key (series_id, index_in_series)
);"""


def dict_depth(dic, level=1):
    if not isinstance(dic, dict) or not dic:
        return level
    return max(dict_depth(dic[key], level + 1) for key in dic)


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, Brep):
            return None
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)
