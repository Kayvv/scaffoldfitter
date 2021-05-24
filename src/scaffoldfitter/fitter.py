"""
Main class for fitting scaffolds.
"""

import json
from opencmiss.utils.maths.vectorops import sub
from opencmiss.utils.zinc.field import assignFieldParameters, createFieldFiniteElementClone, getGroupList, getManagedFieldNames, \
    findOrCreateFieldFiniteElement, findOrCreateFieldGroup, findOrCreateFieldNodeGroup, findOrCreateFieldStoredMeshLocation, \
    getUniqueFieldName, orphanFieldByName
from opencmiss.utils.zinc.finiteelement import evaluateFieldNodesetMean, evaluateFieldNodesetRange, findNodeWithName, getMaximumNodeIdentifier
from opencmiss.utils.zinc.general import ChangeManager
from opencmiss.zinc.context import Context
from opencmiss.zinc.field import Field, FieldFindMeshLocation, FieldGroup
from opencmiss.zinc.result import RESULT_OK, RESULT_WARNING_PART_DONE
from scaffoldfitter.fitterstep import FitterStep
from scaffoldfitter.fitterstepconfig import FitterStepConfig


class Fitter:

    def __init__(self, zincModelFileName : str, zincDataFileName : str):
        """
        :param zincModelFileName: Name of zinc file supplying model to fit.
        :param zincDataFileName: Name of zinc filed supplying data to fit to.
        """
        self._zincModelFileName = zincModelFileName
        self._zincDataFileName = zincDataFileName
        self._context = Context("Scaffoldfitter")
        #self._logger = self._context.getLogger()
        self._region = None
        self._rawDataRegion = None
        self._fieldmodule = None
        self._modelCoordinatesField = None
        self._modelCoordinatesFieldName = None
        self._modelReferenceCoordinatesField = None
        self._dataCoordinatesField = None
        self._dataCoordinatesFieldName = None
        self._mesh = []  # [dimension - 1]
        self._dataHostLocationField = None  # stored mesh location field in highest dimension mesh for all data and markers
        self._dataHostCoordinatesField = None  # embedded field giving host coordinates at data location
        self._dataDeltaField = None  # self._dataHostCoordinatesField - self._markerDataCoordinatesField
        self._dataErrorField = None  # magnitude of _dataDeltaField
        self._dataWeightField = None  # field storing weight of each data and marker point
        self._activeDataGroup = None  # group owning active data
        self._activeDataNodesetGroup = None  # NodesetGroup containing all data and marker points involved in fit
        self._dataProjectionGroupNames = []  # list of group names with data point projections defined
        self._dataProjectionNodeGroupFields = []  # [dimension - 1]
        self._dataProjectionNodesetGroups = []  # [dimension - 1]
        self._dataProjectionDirectionField = None  # for storing original projection direction unit vector
        self._markerGroup = None
        self._markerGroupName = None
        self._markerNodeGroup = None
        self._markerLocationField = None
        self._markerNameField = None
        self._markerCoordinatesField = None
        self._markerDataGroup = None
        self._markerDataCoordinatesField = None
        self._markerDataNameField = None
        self._markerDataLocationGroupField = None
        self._markerDataLocationGroup = None
        self._diagnosticLevel = 0
        # must always have an initial FitterStepConfig - which can never be removed
        self._fitterSteps = []
        fitterStep = FitterStepConfig()
        self.addFitterStep(fitterStep)

    def decodeSettingsJSON(self, s : str, decoder):
        """
        Define Fitter from JSON serialisation output by encodeSettingsJSON.
        :param s: String of JSON encoded Fitter settings.
        :param decoder: decodeJSONFitterSteps(fitter, dct) for decodings FitterSteps.
        """
        # clear fitter steps and load from json; assert later that there is an initial config step
        oldFitterSteps = self._fitterSteps
        self._fitterSteps = []
        dct = json.loads(s, object_hook=lambda dct: decoder(self, dct))
        # self._fitterSteps will already be populated by decoder
        # ensure there is a first config step:
        if (len(self._fitterSteps) > 0) and isinstance(self._fitterSteps[0], FitterStepConfig):
            self._modelCoordinatesFieldName = dct["modelCoordinatesField"]
            self._dataCoordinatesFieldName = dct["dataCoordinatesField"]
            self._markerGroupName = dct["markerGroup"]
            self._diagnosticLevel = dct["diagnosticLevel"]
        else:
            self._fitterSteps = oldFitterSteps
            assert False, "Missing initial config step"

    def encodeSettingsJSON(self) -> str:
        """
        :return: String JSON encoding of Fitter settings.
        """
        dct = {
            "modelCoordinatesField" : self._modelCoordinatesFieldName,
            "dataCoordinatesField" : self._dataCoordinatesFieldName,
            "markerGroup" : self._markerGroupName,
            "diagnosticLevel" : self._diagnosticLevel,
            "fitterSteps" : [ fitterStep.encodeSettingsJSONDict() for fitterStep in self._fitterSteps ]
            }
        return json.dumps(dct, sort_keys=False, indent=4)

    def getInitialFitterStepConfig(self):
        """
        Get first fitter step which must exist and be a FitterStepConfig.
        """
        return self._fitterSteps[0]

    def getInheritFitterStepConfig(self, refFitterStep : FitterStep):
        """
        Get last FitterStepConfig applicable to refFitterStep or None if
        refFitterStep is the first.
        """
        for index in range(self._fitterSteps.index(refFitterStep) - 1, -1, -1):
            if isinstance(self._fitterSteps[index], FitterStepConfig):
                return self._fitterSteps[index]
        return None

    def getActiveFitterStepConfig(self, refFitterStep : FitterStep):
        """
        Get latest FitterStepConfig applicable to refFitterStep.
        Can be itself.
        """
        for index in range(self._fitterSteps.index(refFitterStep), -1, -1):
            if isinstance(self._fitterSteps[index], FitterStepConfig):
                return self._fitterSteps[index]
        assert False, "getActiveFitterStepConfig.  Could not find config."

    def addFitterStep(self, fitterStep : FitterStep, refFitterStep=None):
        """
        :param refFitterStep: FitterStep to insert after, or None to append.
        """
        assert fitterStep.getFitter() == None
        if refFitterStep:
            self._fitterSteps.insert(self._fitterSteps.index(refFitterStep) + 1, fitterStep)
        else:
            self._fitterSteps.append(fitterStep)
        fitterStep._setFitter(self)

    def removeFitterStep(self, fitterStep : FitterStep):
        """
        Remove fitterStep from Fitter.
        :param fitterStep: FitterStep to remove. Must not be initial config.
        :return: Next FitterStep after fitterStep, or previous if None.
        """
        assert fitterStep is not self.getInitialFitterStepConfig()
        index = self._fitterSteps.index(fitterStep)
        self._fitterSteps.remove(fitterStep)
        fitterStep._setFitter(None)
        if index >= len(self._fitterSteps):
            index = -1
        return self._fitterSteps[index]

    def _clearFields(self):
        self._modelCoordinatesField = None
        self._modelReferenceCoordinatesField = None
        self._dataCoordinatesField = None
        self._mesh = []  # [dimension - 1]
        self._dataHostLocationField = None  # stored mesh location field in highest dimension mesh for all data and markers
        self._dataHostCoordinatesField = None  # embedded field giving host coordinates at data location
        self._dataDeltaField = None  # self._dataHostCoordinatesField - self._markerDataCoordinatesField
        self._dataErrorField = None  # magnitude of _dataDeltaField
        self._dataWeightField = None  # field storing weight of each data and marker point
        self._activeDataGroup = None  # group owning active data
        self._activeDataNodesetGroup = None  # NodesetGroup containing all data and marker points involved in fit
        self._dataProjectionGroupNames = []  # list of group names with data point projections defined
        self._dataProjectionNodeGroupFields = []  # [dimension - 1]
        self._dataProjectionNodesetGroups = []  # [dimension - 1]
        self._dataProjectionDirectionField = None  # for storing original projection direction unit vector
        self._markerGroup = None
        self._markerGroupName = None
        self._markerNodeGroup = None
        self._markerLocationField = None
        self._markerNameField = None
        self._markerCoordinatesField = None
        self._markerDataGroup = None
        self._markerDataCoordinatesField = None
        self._markerDataNameField = None
        self._markerDataLocationGroupField = None
        self._markerDataLocationGroup = None

    def load(self):
        """
        Read model and data and define fit fields and data.
        Can call again to reset fit, after parameters have changed.
        """
        self._clearFields()
        self._region = self._context.createRegion()
        self._fieldmodule = self._region.getFieldmodule()
        self._rawDataRegion = self._region.createChild("raw_data")
        self._loadModel()
        self._loadData()
        self._defineDataProjectionFields()
        # get centre and scale of data coordinates to manage fitting tolerances and steps
        datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
        minimums, maximums = evaluateFieldNodesetRange(self._dataCoordinatesField, datapoints)
        self._dataCentre = [ 0.5*(minimums[c] + maximums[c]) for c in range(3) ]
        self._dataScale = max((maximums[c] - minimums[c]) for c in range(3))
        if self._diagnosticLevel > 0:
            print("Load data: data coordinates centre ", self._dataCentre)
            print("Load data: data coordinates scale ", self._dataScale)
        for step in self._fitterSteps:
            step.setHasRun(False)
        self._fitterSteps[0].run()  # initial config step will calculate data projections

    def getDataCentre(self):
        """
        :return: Pre-calculated centre of data on [ x, y, z].
        """
        return self._dataScale

    def getDataScale(self):
        """
        :return: Pre-calculated maximum span of data on x, y, or z.
        """
        return self._dataScale

    def _loadModel(self):
        result = self._region.readFile(self._zincModelFileName)
        assert result == RESULT_OK, "Failed to load model file" + str(self._zincModelFileName)
        self._mesh = [ self._fieldmodule.findMeshByDimension(d + 1) for d in range(3) ]
        self._discoverModelCoordinatesField()

    def _defineCommonDataFields(self):
        '''
        Defines self._dataHostCoordinatesField to gives the value of self._modelCoordinatesField at
        embedded location self._dataHostLocationField.
        Need to call again if self._modelCoordinatesField is changed.
        '''
        # need to store all data + marker locations in top-level elements for NEWTON objective
        # in future may want to support mixed dimension top-level elements
        if not (self._modelCoordinatesField and self._dataCoordinatesField):
            return  # on first load, can't call until setModelCoordinatesField and setDataCoordinatesField
        with ChangeManager(self._fieldmodule):
            mesh = self.getHighestDimensionMesh();
            datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
            self._dataHostLocationField = findOrCreateFieldStoredMeshLocation(self._fieldmodule, mesh, "data_location_" + mesh.getName(), managed=False)
            self._dataHostCoordinatesField = self._fieldmodule.createFieldEmbedded(self._modelCoordinatesField, self._dataHostLocationField)
            self._dataHostCoordinatesField.setName(getUniqueFieldName(self._fieldmodule, "data_host_coordinates"))
            self._dataDeltaField = self._dataHostCoordinatesField - self._dataCoordinatesField
            self._dataDeltaField.setName(getUniqueFieldName(self._fieldmodule, "data_delta"))
            self._dataErrorField = self._fieldmodule.createFieldMagnitude(self._dataDeltaField)
            self._dataErrorField.setName(getUniqueFieldName(self._fieldmodule, "data_error"))
            # store weights per-point so can maintain variable weights by for marker and data by group, dimension of host
            self._dataWeightField = findOrCreateFieldFiniteElement(self._fieldmodule, "data_weight", components_count=1)
            self._activeDataGroup = findOrCreateFieldGroup(self._fieldmodule, "active_data");
            self._activeDataNodesetGroup = findOrCreateFieldNodeGroup(self._activeDataGroup, datapoints).getNodesetGroup();

    def _loadData(self):
        """
        Load zinc data file into self._rawDataRegion.
        Rename data groups to exactly match model groups where they differ by case and whitespace only.
        Transfer data points (and converted nodes) into self._region.
        """
        result = self._rawDataRegion.readFile(self._zincDataFileName)
        assert result == RESULT_OK, "Failed to load data file " + str(self._zincDataFileName)
        fieldmodule = self._rawDataRegion.getFieldmodule()
        with ChangeManager(fieldmodule):
            # rename data groups to match model
            # future: match with annotation terms
            modelGroupNames = [ group.getName() for group in getGroupList(self._fieldmodule) ]
            writeDiagnostics = self.getDiagnosticLevel() > 0
            for dataGroup in getGroupList(fieldmodule):
                dataGroupName = dataGroup.getName()
                compareName = dataGroupName.strip().casefold()
                for modelGroupName in modelGroupNames:
                    if modelGroupName == dataGroupName:
                        if writeDiagnostics:
                            print("Load data: Data group '" + dataGroupName + "' found in model")
                        break
                    elif modelGroupName.strip().casefold() == compareName:
                        result = dataGroup.setName(modelGroupName)
                        if result == RESULT_OK:
                            if writeDiagnostics:
                                print("Load data: Data group '" + dataGroupName + "' found in model as '" + modelGroupName + "'. Renaming to match.")
                        else:
                            print("Error: Load data: Data group '" + dataGroupName + "' found in model as '" + modelGroupName + "'. Renaming to match FAILED.")
                            if fieldmodule.findFieldByName(modelGroupName).isValid():
                                print("    Reason: field of that name already exists.")
                        break
                else:
                    if writeDiagnostics:
                        print("Load data: Data group '" + dataGroupName + "' not found in model")
            # if there are both nodes and datapoints, offset datapoint identifiers to ensure different
            nodes = fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
            if nodes.getSize() > 0:
                datapoints = fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
                if datapoints.getSize() > 0:
                    maximumDatapointIdentifier = max(0, getMaximumNodeIdentifier(datapoints))
                    maximumNodeIdentifier = max(0, getMaximumNodeIdentifier(nodes))
                    # this assumes identifiers are in low ranges and can be improved if there is a problem:
                    identifierOffset = 100000
                    while (maximumDatapointIdentifier > identifierOffset) or (maximumNodeIdentifier > identifierOffset):
                        assert identifierOffset < 1000000000, "Invalid node and datapoint identifier ranges"
                        identifierOffset *= 10
                    while True:
                        # logic relies on datapoints being in identifier order
                        datapoint = datapoints.createNodeiterator().next()
                        identifier = datapoint.getIdentifier()
                        if identifier >= identifierOffset:
                            break;
                        result = datapoint.setIdentifier(identifier + identifierOffset)
                        assert result == RESULT_OK, "Failed to offset datapoint identifier"
                # transfer nodes as datapoints to self._region
                sir = self._rawDataRegion.createStreaminformationRegion()
                srm = sir.createStreamresourceMemory()
                sir.setResourceDomainTypes(srm, Field.DOMAIN_TYPE_NODES)
                self._rawDataRegion.write(sir)
                result, buffer = srm.getBuffer()
                assert result == RESULT_OK, "Failed to write nodes"
                buffer = buffer.replace(bytes("!#nodeset nodes", "utf-8"), bytes("!#nodeset datapoints", "utf-8"))
                sir = self._region.createStreaminformationRegion()
                srm = sir.createStreamresourceMemoryBuffer(buffer)
                result = self._region.read(sir)
                assert result == RESULT_OK, "Failed to load nodes as datapoints"
        # transfer datapoints to self._region
        sir = self._rawDataRegion.createStreaminformationRegion()
        srm = sir.createStreamresourceMemory()
        sir.setResourceDomainTypes(srm, Field.DOMAIN_TYPE_DATAPOINTS)
        self._rawDataRegion.write(sir)
        result, buffer = srm.getBuffer()
        assert result == RESULT_OK, "Failed to write datapoints"
        sir = self._region.createStreaminformationRegion()
        srm = sir.createStreamresourceMemoryBuffer(buffer)
        result = self._region.read(sir)
        assert result == RESULT_OK, "Failed to load datapoints"
        self._discoverDataCoordinatesField()
        self._discoverMarkerGroup()

    def run(self, endStep=None, modelFileNameStem=None):
        """
        Run either all remaining fitter steps or up to specified end step.
        :param endStep: Last fitter step to run, or None to run all.
        :param modelFilename: Filename stem for writing intermediate model files.
        :return: True if reloaded (so scene changed), False if not.
        """
        if not endStep:
            endStep = self._fitterSteps[-1]
        endIndex = self._fitterSteps.index(endStep)
        # reload only if necessary
        if endStep.hasRun() and (endIndex < (len(self._fitterSteps) - 1)) and self._fitterSteps[endIndex + 1].hasRun():
            # re-load to get back to current state
            self.load()
            for index in range(1, endIndex + 1):
                self._fitterSteps[index].run(modelFileNameStem + str(index) if modelFileNameStem else None)
            return True
        if endIndex == 0:
            endStep.run()  # force re-run initial config
        else:
            # run from current point up to step
            for index in range(1, endIndex + 1):
                if not self._fitterSteps[index].hasRun():
                    self._fitterSteps[index].run(modelFileNameStem + str(index) if modelFileNameStem else None)
        return False

    def getDataCoordinatesField(self):
        return self._dataCoordinatesField

    def setDataCoordinatesField(self, dataCoordinatesField : Field):
        if dataCoordinatesField == self._dataCoordinatesField:
            return
        finiteElementField = dataCoordinatesField.castFiniteElement()
        assert finiteElementField.isValid() and (finiteElementField.getNumberOfComponents() == 3)
        self._dataCoordinatesFieldName = dataCoordinatesField.getName()
        self._dataCoordinatesField = finiteElementField
        self._defineCommonDataFields()
        self._calculateMarkerDataLocations()  # needed to assign to self._dataCoordinatesField

    def setDataCoordinatesFieldByName(self, dataCoordinatesFieldName):
        self.setDataCoordinatesField(self._fieldmodule.findFieldByName(dataCoordinatesFieldName))

    def _discoverDataCoordinatesField(self):
        """
        Choose default dataCoordinates field.
        """
        self._dataCoordinatesField = None
        field = None
        if self._dataCoordinatesFieldName:
            field = self._fieldmodule.findFieldByName(self._dataCoordinatesFieldName)
        if not ((field) and field.isValid()):
            datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
            datapoint = datapoints.createNodeiterator().next()
            if datapoint.isValid():
                fieldcache = self._fieldmodule.createFieldcache()
                fieldcache.setNode(datapoint)
                fielditer = self._fieldmodule.createFielditerator()
                field = fielditer.next()
                while field.isValid():
                    if field.isTypeCoordinate() and (field.getNumberOfComponents() == 3) and (field.castFiniteElement().isValid()):
                        if field.isDefinedAtLocation(fieldcache):
                            break;
                    field = fielditer.next()
                else:
                    field = None
        self.setDataCoordinatesField(field)

    def getMarkerGroup(self):
        return self._markerGroup

    def setMarkerGroup(self, markerGroup : Field):
        self._markerGroup = None
        self._markerGroupName = None
        self._markerNodeGroup = None
        self._markerLocationField = None
        self._markerCoordinatesField = None
        self._markerNameField = None
        self._markerDataGroup = None
        self._markerDataCoordinatesField = None
        self._markerDataNameField = None
        self._markerDataLocationGroupField = None
        self._markerDataLocationGroup = None
        if not markerGroup:
            return
        fieldGroup = markerGroup.castGroup()
        assert fieldGroup.isValid()
        self._markerGroup = fieldGroup
        self._markerGroupName = markerGroup.getName()
        nodes = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_NODES)
        self._markerNodeGroup = self._markerGroup.getFieldNodeGroup(nodes).getNodesetGroup()
        if self._markerNodeGroup.isValid():
            node = self._markerNodeGroup.createNodeiterator().next()
            if node.isValid():
                fieldcache = self._fieldmodule.createFieldcache()
                fieldcache.setNode(node)
                fielditer = self._fieldmodule.createFielditerator()
                field = fielditer.next()
                while field.isValid():
                    if field.isDefinedAtLocation(fieldcache):
                        if (not self._markerLocationField) and field.castStoredMeshLocation().isValid():
                            self._markerLocationField = field
                        elif (not self._markerNameField) and (field.getValueType() == Field.VALUE_TYPE_STRING):
                            self._markerNameField = field
                    field = fielditer.next()
                self._updateMarkerCoordinatesField()
        else:
            self._markerNodeGroup = None
        datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
        self._markerDataGroup = self._markerGroup.getFieldNodeGroup(datapoints).getNodesetGroup()
        if self._markerDataGroup.isValid():
            datapoint = self._markerDataGroup.createNodeiterator().next()
            if datapoint.isValid():
                fieldcache = self._fieldmodule.createFieldcache()
                fieldcache.setNode(datapoint)
                fielditer = self._fieldmodule.createFielditerator()
                field = fielditer.next()
                while field.isValid():
                    if field.isDefinedAtLocation(fieldcache):
                        if (not self._markerDataCoordinatesField) and field.isTypeCoordinate() and \
                                (field.getNumberOfComponents() == 3) and (field.castFiniteElement().isValid()):
                            self._markerDataCoordinatesField = field
                        elif (not self._markerDataNameField) and (field.getValueType() == Field.VALUE_TYPE_STRING):
                            self._markerDataNameField = field
                    field = fielditer.next()
        else:
            self._markerDataGroup = None
        self._calculateMarkerDataLocations()

    def assignDataWeights(self, lineWeight, markerWeight):
        '''
        Assign values of the weight field for all data and marker points.
        Assigns value 1.0 to datapoints embedded in 2-D surfaces, multiplying
        by other factors for other geometries.
        :param lineWeight: Multiplier for data embedded in 1-D lines.
        :param markerWeight: Multiplier for marker points data.
        '''
        # Future: divide by linear data scale?
        # Future: divide by number of data points?
        with ChangeManager(self._fieldmodule):
            for d in range(2):  # dimension 1, 2
                if self._dataProjectionNodesetGroups[d]:
                    fieldassignment = self._dataWeightField.createFieldassignment(self._fieldmodule.createFieldConstant(lineWeight if (d == 0) else 1.0))
                    fieldassignment.setNodeset(self._dataProjectionNodesetGroups[d]);
                    result = fieldassignment.assign();
                    if result != RESULT_OK:
                        print('Incomplete assignment of data weight ' + str(d + 1) + 'D. Result', result)
            if self._markerDataLocationGroup:
                fieldassignment = self._dataWeightField.createFieldassignment(self._fieldmodule.createFieldConstant(markerWeight))
                fieldassignment.setNodeset(self._markerDataLocationGroup);
                result = fieldassignment.assign();
                if result != RESULT_OK:
                    print('Incomplete assignment of marker data weight', result)
            del fieldassignment

    def setMarkerGroupByName(self, markerGroupName):
        self.setMarkerGroup(self._fieldmodule.findFieldByName(markerGroupName))

    def getDataHostLocationField(self):
        return self._dataHostLocationField;

    def getDataHostCoordinatesField(self):
        return self._dataHostCoordinatesField;

    def getDataDeltaField(self):
        return self._dataDeltaField;

    def getDataWeightField(self):
        return self._dataWeightField;

    def getActiveDataNodesetGroup(self):
        return self._activeDataNodesetGroup;

    def getMarkerDataFields(self):
        """
        Only call if markerGroup exists.
        :return: markerDataGroup, markerDataCoordinates, markerDataName
        """
        return self._markerDataGroup, self._markerDataCoordinatesField, self._markerDataNameField

    def getMarkerDataLocationFields(self):
        """
        Get fields giving marker location coordinates and delta on the data points (copied from nodes).
        Only call if markerGroup exists.
        :return: markerDataLocation, markerDataLocationCoordinates, markerDataDelta
        """
        # these are now common:
        return self._dataHostLocationField, self._dataHostCoordinatesField, self._dataDeltaField

    def getMarkerModelFields(self):
        """
        Only call if markerGroup exists.
        :return: markerNodeGroup, markerLocation, markerCoordinates, markerName
        """
        return self._markerNodeGroup, self._markerLocationField, self._markerCoordinatesField, self._markerNameField

    def _calculateMarkerDataLocations(self):
        """
        Called when markerGroup exists.
        Find matching marker mesh locations for marker data points.
        Only finds matching location where there is one datapoint and one node
        for each name in marker group.
        Defines datapoint group self._markerDataLocationGroup to contain those with locations.
        """
        self._markerDataLocationGroupField = None
        self._markerDataLocationGroup = None
        if not (self._markerDataGroup and self._markerDataNameField and self._markerNodeGroup and self._markerLocationField and self._markerNameField):
            return

        markerPrefix = self._markerGroupName
        # assume marker locations are in highest dimension mesh
        mesh = self.getHighestDimensionMesh()
        datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
        meshDimension = mesh.getDimension()
        with ChangeManager(self._fieldmodule):
            fieldcache = self._fieldmodule.createFieldcache()
            self._markerDataLocationGroupField = self._fieldmodule.createFieldNodeGroup(datapoints)
            self._markerDataLocationGroupField.setName(getUniqueFieldName(self._fieldmodule, markerPrefix + "_data_location_group"))
            self._markerDataLocationGroup = self._markerDataLocationGroupField.getNodesetGroup()
            nodetemplate = self._markerDataGroup.createNodetemplate()
            nodetemplate.defineField(self._dataHostLocationField)
            componentsCount = self._markerDataCoordinatesField.getNumberOfComponents()
            defineDataCoordinates = self._markerDataCoordinatesField != self._dataCoordinatesField;
            if defineDataCoordinates:
                # define dataCoordinates on marker points for combined objective, and assign below
                assert self._dataCoordinatesField.isValid()
                nodetemplate.defineField(self._dataCoordinatesField)
            # need to define storage for marker data weight, but don't assign here
            nodetemplate.defineField(self._dataWeightField)
            datapointIter = self._markerDataGroup.createNodeiterator()
            datapoint = datapointIter.next()
            while datapoint.isValid():
                fieldcache.setNode(datapoint)
                name = self._markerDataNameField.evaluateString(fieldcache)
                # if this is the only datapoint with name:
                if name and findNodeWithName(self._markerDataGroup, self._markerDataNameField, name, ignore_case=True, strip_whitespace=True):
                    result, dataCoordinates = self._markerDataCoordinatesField.evaluateReal(fieldcache, componentsCount);
                    node = findNodeWithName(self._markerNodeGroup, self._markerNameField, name, ignore_case=True, strip_whitespace=True)
                    if (result == RESULT_OK) and node:
                        fieldcache.setNode(node)
                        element, xi = self._markerLocationField.evaluateMeshLocation(fieldcache, meshDimension)
                        if element.isValid() and (result == RESULT_OK):
                            datapoint.merge(nodetemplate)
                            fieldcache.setNode(datapoint)
                            self._dataHostLocationField.assignMeshLocation(fieldcache, element, xi)
                            if defineDataCoordinates:
                                result = self._dataCoordinatesField.assignReal(fieldcache, dataCoordinates)
                            self._markerDataLocationGroup.addNode(datapoint)
                datapoint = datapointIter.next()
            del fieldcache
        # Warn about marker points without a location in model
        markerDataGroupSize = self._markerDataGroup.getSize()
        markerDataLocationGroupSize = self._markerDataLocationGroup.getSize()
        markerNodeGroupSize = self._markerNodeGroup.getSize()
        if self.getDiagnosticLevel() > 0:
            if markerDataLocationGroupSize < markerDataGroupSize:
                print("Warning: Only " + str(markerDataLocationGroupSize) + " of " + str(markerDataGroupSize) + " marker data points have model locations")
            if markerDataLocationGroupSize < markerNodeGroupSize:
                print("Warning: Only " + str(markerDataLocationGroupSize) + " of " + str(markerNodeGroupSize) + " marker model locations used")

    def _discoverMarkerGroup(self):
        self._markerGroup = None
        self._markerNodeGroup = None
        self._markerLocationField = None
        self._markerNameField = None
        self._markerCoordinatesField = None
        markerGroup = self._fieldmodule.findFieldByName(self._markerGroupName if self._markerGroupName else "marker").castGroup()
        if not markerGroup.isValid():
            markerGroup = None
        self.setMarkerGroup(markerGroup)

    def _updateMarkerCoordinatesField(self):
        if self._modelCoordinatesField and self._markerLocationField:
            with ChangeManager(self._fieldmodule):
                markerPrefix = self._markerGroup.getName()
                self._markerCoordinatesField = self._fieldmodule.createFieldEmbedded(self._modelCoordinatesField, self._markerLocationField)
                self._markerCoordinatesField.setName(getUniqueFieldName(self._fieldmodule, markerPrefix + "_coordinates"))
        else:
            self._markerCoordinatesField = None

    def getModelCoordinatesField(self):
        return self._modelCoordinatesField

    def getModelReferenceCoordinatesField(self):
        return self._modelReferenceCoordinatesField

    def setModelCoordinatesField(self, modelCoordinatesField : Field):
        if modelCoordinatesField == self._modelCoordinatesField:
            return
        finiteElementField = modelCoordinatesField.castFiniteElement()
        assert finiteElementField.isValid() and (finiteElementField.getNumberOfComponents() == 3)
        self._modelCoordinatesField = finiteElementField
        self._modelCoordinatesFieldName = modelCoordinatesField.getName()
        modelReferenceCoordinatesFieldName = "reference_" + self._modelCoordinatesField.getName();
        orphanFieldByName(self._fieldmodule, modelReferenceCoordinatesFieldName);
        self._modelReferenceCoordinatesField = createFieldFiniteElementClone(self._modelCoordinatesField, modelReferenceCoordinatesFieldName)
        self._defineCommonDataFields()
        self._updateMarkerCoordinatesField

    def setModelCoordinatesFieldByName(self, modelCoordinatesFieldName):
        self.setModelCoordinatesField(self._fieldmodule.findFieldByName(modelCoordinatesFieldName))

    def _discoverModelCoordinatesField(self):
        """
        Choose default modelCoordinates field.
        """
        self._modelCoordinatesField = None
        self._modelReferenceCoordinatesField = None
        field = None
        if self._modelCoordinatesFieldName:
            field = self._fieldmodule.findFieldByName(self._modelCoordinatesFieldName)
        else:
            mesh = self.getHighestDimensionMesh()
            element = mesh.createElementiterator().next()
            if element.isValid():
                fieldcache = self._fieldmodule.createFieldcache()
                fieldcache.setElement(element)
                fielditer = self._fieldmodule.createFielditerator()
                field = fielditer.next()
                while field.isValid():
                    if field.isTypeCoordinate() and (field.getNumberOfComponents() == 3) and (field.castFiniteElement().isValid()):
                        if field.isDefinedAtLocation(fieldcache):
                            break;
                    field = fielditer.next()
                else:
                    field = None
        if field:
            self.setModelCoordinatesField(field)

    def _defineDataProjectionFields(self):
        self._dataProjectionGroupNames = []
        self._dataProjectionNodeGroupFields = []
        self._dataProjectionNodesetGroups = []
        with ChangeManager(self._fieldmodule):
            datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
            for d in range(2):
                mesh = self._mesh[d]  # mesh1d, mesh2d
                field = self._fieldmodule.createFieldNodeGroup(datapoints)
                field.setName(getUniqueFieldName(self._fieldmodule, "data_projection_group_" + mesh.getName()))
                self._dataProjectionNodeGroupFields.append(field)
                self._dataProjectionNodesetGroups.append(field.getNodesetGroup())
            self._dataProjectionDirectionField = findOrCreateFieldFiniteElement(self._fieldmodule, "data_projection_direction",
                components_count = 3, component_names = [ "x", "y", "z" ])

    def calculateGroupDataProjections(self, fieldcache, group, dataGroup, meshGroup, meshLocation, activeFitterStepConfig : FitterStepConfig):
        """
        Project data points for group. Assumes called while ChangeManager is active for fieldmodule.
        :param group: The FieldGroup being fitted (parent of dataGroup, meshGroup).
        :param dataGroup: Nodeset group containing data points to project.
        :param meshGroup: MeshGroup containing surfaces/lines to project onto.
        :param meshLocation: FieldStoredMeshLocation to store found location in on highest dimension mesh.
        :param activeFitterStepConfig: Where to get current projection modes from.
        """
        groupName = group.getName()
        dimension = meshGroup.getDimension()
        dataProjectionNodesetGroup = self._dataProjectionNodesetGroups[dimension - 1]
        sizeBefore = dataProjectionNodesetGroup.getSize()
        dataCoordinates = self._dataCoordinatesField
        dataProportion = activeFitterStepConfig.getGroupDataProportion(groupName)[0]
        if activeFitterStepConfig.isProjectionCentreGroups():
            # get geometric centre of dataGroup
            dataCentreField = self._fieldmodule.createFieldNodesetMean(dataCoordinates, dataGroup)
            result, dataCentre = dataCentreField.evaluateReal(fieldcache, dataCoordinates.getNumberOfComponents())
            if result != RESULT_OK:
                print("Error: Centre Groups projection failed to get mean coordinates of data for group " + groupName)
                return
            #print("Centre Groups dataCentre", dataCentre)
            # get geometric centre of meshGroup
            meshGroupCoordinatesIntegral = self._fieldmodule.createFieldMeshIntegral(self._modelCoordinatesField, self._modelCoordinatesField, meshGroup)
            meshGroupCoordinatesIntegral.setNumbersOfPoints([3])
            meshGroupArea = self._fieldmodule.createFieldMeshIntegral(self._fieldmodule.createFieldConstant([1.0]), self._modelCoordinatesField, meshGroup)
            meshGroupArea.setNumbersOfPoints([3])
            result1, coordinatesIntegral = meshGroupCoordinatesIntegral.evaluateReal(fieldcache, self._modelCoordinatesField.getNumberOfComponents())
            result2, area = meshGroupArea.evaluateReal(fieldcache, 1)
            if (result1 != RESULT_OK) or (result2 != RESULT_OK) or (area <= 0.0):
                print("Error: Centre Groups projection failed to get mean coordinates of mesh for group " + groupName)
                return
            meshCentre = [ s/area for s in coordinatesIntegral ]
            #print("Centre Groups meshCentre", meshCentre)
            # offset dataCoordinates to make dataCentre coincide with meshCentre
            dataCoordinates = dataCoordinates + self._fieldmodule.createFieldConstant(sub(meshCentre, dataCentre))

        # find nearest locations on 1-D or 2-D feature but store on highest dimension mesh
        highestDimensionMesh = self.getHighestDimensionMesh()
        highestDimension = highestDimensionMesh.getDimension();
        findLocation = self._fieldmodule.createFieldFindMeshLocation(dataCoordinates, self._modelCoordinatesField, highestDimensionMesh)
        assert RESULT_OK == findLocation.setSearchMesh(meshGroup)
        findLocation.setSearchMode(FieldFindMeshLocation.SEARCH_MODE_NEAREST)
        nodeIter = dataGroup.createNodeiterator()
        node = nodeIter.next()
        dataProportionCounter = 0.5
        while node.isValid():
            dataProportionCounter += dataProportion
            if dataProportionCounter >= 1.0:
                dataProportionCounter -= 1.0
                fieldcache.setNode(node)
                element, xi = findLocation.evaluateMeshLocation(fieldcache, highestDimension)
                if element.isValid():
                    result = meshLocation.assignMeshLocation(fieldcache, element, xi)
                    assert result == RESULT_OK, "Error: Failed to assign data projection mesh location for group " + groupName
                    dataProjectionNodesetGroup.addNode(node)
            node = nodeIter.next()
        pointsProjected = dataProjectionNodesetGroup.getSize() - sizeBefore
        if pointsProjected < dataGroup.getSize():
            if self.getDiagnosticLevel() > 0:
                print("Warning: Only " + str(pointsProjected) + " of " + str(dataGroup.getSize()) + " data points projected for group " + groupName)
        # add to active group
        self._activeDataNodesetGroup.addNodesConditional(self._dataProjectionNodeGroupFields[dimension - 1]);
        return 

    def getGroupDataProjectionNodesetGroup(self, group : FieldGroup):
        '''
        :return: Data NodesetGroup containing points for projection of group, otherwise None.
        '''
        datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
        dataGroupField = group.getFieldNodeGroup(datapoints)
        if dataGroupField.isValid():
            dataGroup = dataGroupField.getNodesetGroup()
            if dataGroup.getSize() > 0:
                return dataGroup
        return None

    def getGroupDataProjectionMeshGroup(self, group : FieldGroup):
        '''
        :return: 2D if not 1D meshGroup containing elements for projecting data in group, otherwise None.
        '''
        for dimension in range(2, 0, -1):
            elementGroupField = group.getFieldElementGroup(self._mesh[dimension - 1])
            if elementGroupField.isValid():
                meshGroup = elementGroupField.getMeshGroup()
                if meshGroup.getSize() > 0:
                    return meshGroup
        return None

    def calculateDataProjections(self, fitterStep : FitterStep):
        """
        Find projections of datapoints' coordinates onto model coordinates,
        by groups i.e. from datapoints group onto matching 2-D or 1-D mesh group.
        Calculate and store projection direction unit vector.
        """
        assert self._dataCoordinatesField and self._modelCoordinatesField
        activeFitterStepConfig = self.getActiveFitterStepConfig(fitterStep)
        with ChangeManager(self._fieldmodule):
            # build group of active data and marker points
            self._activeDataNodesetGroup.removeAllNodes();
            if self._markerDataLocationGroupField:
                self._activeDataNodesetGroup.addNodesConditional(self._markerDataLocationGroupField)

            findMeshLocation = None
            datapoints = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS)
            fieldcache = self._fieldmodule.createFieldcache()
            for d in range(2):
                self._dataProjectionNodesetGroups[d].removeAllNodes()
            groups = getGroupList(self._fieldmodule)
            for group in groups:
                if group == self._activeDataGroup:
                    continue
                groupName = group.getName()
                dataGroup = self.getGroupDataProjectionNodesetGroup(group)
                if not dataGroup:
                    continue
                meshGroup = self.getGroupDataProjectionMeshGroup(group)
                if not meshGroup:
                    if self.getDiagnosticLevel() > 0:
                        if group != self._markerGroup:
                            print("Warning: Cannot project data for group " + groupName + " as no matching mesh group")
                    continue
                dimension = meshGroup.getDimension()
                if groupName not in self._dataProjectionGroupNames:
                    self._dataProjectionGroupNames.append(groupName)  # so only define mesh location, or warn once
                    fieldcache.setNode(dataGroup.createNodeiterator().next())
                    if not self._dataCoordinatesField.isDefinedAtLocation(fieldcache):
                        if self.getDiagnosticLevel() > 0:
                            print("Warning: Cannot project data for group " + groupName + " as field " + self._dataCoordinatesField.getName() + " is not defined on data")
                        continue
                    # define self._dataHostLocationField and self._dataProjectionDirectionField on data Group:
                    nodetemplate = datapoints.createNodetemplate()
                    nodetemplate.defineField(self._dataHostLocationField)
                    # need to define storage for marker data weight, but don't assign here
                    nodetemplate.defineField(self._dataWeightField)
                    nodetemplate.defineField(self._dataProjectionDirectionField)
                    nodeIter = dataGroup.createNodeiterator()
                    node = nodeIter.next()
                    while node.isValid():
                        result = node.merge(nodetemplate)
                        #print("node",node.getIdentifier(),"result",result)
                        node = nodeIter.next()
                    del nodetemplate
                self.calculateGroupDataProjections(fieldcache, group, dataGroup, meshGroup, self._dataHostLocationField, activeFitterStepConfig)

            # Store data projection directions
            for dimension in range(1, 3):
                nodesetGroup = self._dataProjectionNodesetGroups[dimension - 1]
                if nodesetGroup.getSize() > 0:
                    fieldassignment = self._dataProjectionDirectionField.createFieldassignment(
                        self._fieldmodule.createFieldNormalise(self._dataDeltaField))
                    fieldassignment.setNodeset(nodesetGroup)
                    result = fieldassignment.assign()
                    assert result in [ RESULT_OK, RESULT_WARNING_PART_DONE ], \
                        "Error:  Failed to assign data projection directions for dimension " + str(dimension)
                    del fieldassignment

            if self.getDiagnosticLevel() > 0:
                # Warn about unprojected points
                unprojectedDatapoints = self._fieldmodule.createFieldNodeGroup(datapoints).getNodesetGroup()
                unprojectedDatapoints.addNodesConditional(self._fieldmodule.createFieldIsDefined(self._dataCoordinatesField))
                for d in range(2):
                    unprojectedDatapoints.removeNodesConditional(self._dataProjectionNodeGroupFields[d])
                unprojectedCount = unprojectedDatapoints.getSize()
                if unprojectedCount > 0:
                    print("Warning: " + str(unprojectedCount) + " data points with data coordinates have not been projected")
                del unprojectedDatapoints

            # remove temporary objects before ChangeManager exits
            del fieldcache

    def getDataProjectionDirectionField(self):
        return self._dataProjectionDirectionField

    def getDataProjectionNodeGroupField(self, dimension):
        assert 1 <= dimension <= 2
        return self._dataProjectionNodeGroupFields[dimension - 1]

    def getDataProjectionNodesetGroup(self, dimension):
        assert 1 <= dimension <= 2
        return self._dataProjectionNodesetGroups[dimension - 1]

    def getDataProjectionCoordinatesField(self, dimension):
        """
        :return: Field giving coordinates of projections of data points on mesh of dimension.
        GRC remove - only used in GeometricFitStep
        """
        assert 1 <= dimension <= 2
        return self._dataHostCoordinatesField

    def getDataProjectionDeltaField(self, dimension):
        """
        :return: Field giving delta coordinates (projection coordinates - data coordinates)
        for data points on mesh of dimension.
        GRC remove - only used in GeometricFitStep
        """
        assert 1 <= dimension <= 2
        return self._dataDeltaField

    def getDataProjectionErrorField(self, dimension):
        """
        :return: Field giving magnitude of data point delta coordinates.
        GRC remove - only used in GeometricFitStep
        """
        assert 1 <= dimension <= 2
        return self._dataErrorField

    def getMarkerDataLocationGroupField(self):
        return self._markerDataLocationGroupField

    def getMarkerDataLocationNodesetGroup(self):
        return self._markerDataLocationGroup

    def getMarkerDataLocationField(self):
        return self._markerDataLocationField

    def getContext(self):
        return self._context

    def getRegion(self):
        return self._region

    def getFieldmodule(self):
        return self._fieldmodule

    def getFitterSteps(self):
        return self._fitterSteps

    def getMesh(self, dimension):
        assert 1 <= dimension <= 3
        return self._mesh[dimension - 1]

    def getHighestDimensionMesh(self):
        """
        :return: Highest dimension mesh with elements in it, or None if none.
        """
        for d in range(2, -1, -1):
            mesh = self._mesh[d]
            if mesh.getSize() > 0:
                return mesh
        return None

    def evaluateNodeGroupMeanCoordinates(self, groupName, coordinatesFieldName, isData = False):
        group = self._fieldmodule.findFieldByName(groupName).castGroup()
        assert group.isValid()
        nodeset = self._fieldmodule.findNodesetByFieldDomainType(Field.DOMAIN_TYPE_DATAPOINTS if isData else Field.DOMAIN_TYPE_NODES)
        nodesetGroup = group.getFieldNodeGroup(nodeset).getNodesetGroup()
        assert nodesetGroup.isValid()
        coordinates = self._fieldmodule.findFieldByName(coordinatesFieldName)
        return evaluateFieldNodesetMean(coordinates, nodesetGroup)

    def getDiagnosticLevel(self):
        return self._diagnosticLevel

    def setDiagnosticLevel(self, diagnosticLevel):
        """
        :param diagnosticLevel: 0 = no diagnostic messages. 1 = Information and warning messages. 2 = Also optimisation reports.
        """
        assert diagnosticLevel >= 0
        self._diagnosticLevel = diagnosticLevel

    def updateModelReferenceCoordinates(self):
        assignFieldParameters(self._modelReferenceCoordinatesField, self._modelCoordinatesField)

    def writeModel(self, modelFileName):
        """
        Write model nodes and elements excluding unmanaged fields to file.
        """
        sir = self._region.createStreaminformationRegion()
        sir.setRecursionMode(sir.RECURSION_MODE_OFF)
        srf = sir.createStreamresourceFile(modelFileName)
        sir.setResourceFieldNames(srf, getManagedFieldNames(self._fieldmodule))
        sir.setResourceDomainTypes(srf, Field.DOMAIN_TYPE_NODES | Field.DOMAIN_TYPE_MESH1D | Field.DOMAIN_TYPE_MESH2D | Field.DOMAIN_TYPE_MESH3D)
        result = self._region.write(sir)
        assert result == RESULT_OK

    def writeData(self, fileName):
        sir = self._region.createStreaminformationRegion()
        sir.setRecursionMode(sir.RECURSION_MODE_OFF)
        sr = sir.createStreamresourceFile(fileName)
        sir.setResourceDomainTypes(sr, Field.DOMAIN_TYPE_DATAPOINTS)
        self._region.write(sir)
